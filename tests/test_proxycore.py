# -*- coding: utf-8 -*-
import unittest
import threading
from unittest import mock

from api import Api
from core import codex_proxy, routing


class ProxyCoreConfirmationTests(unittest.TestCase):
    def setUp(self):
        for name in ('sync', 'restore'):
            patcher = mock.patch.object(codex_proxy, name)
            setattr(self, 'codex_' + name, patcher.start())
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.codex_sync.assert_not_called()
        self.codex_restore.assert_not_called()

    def _manager(self):
        manager = routing.RoutingManager()
        manager._native_service = mock.Mock()
        manager._verify_runtime_egress = mock.Mock()
        manager.status = mock.Mock(return_value={'running': False})
        return manager

    def test_fast_enable_rejects_inconsistent_native_state(self):
        manager = self._manager()
        manager._native_service.set_system_proxy_enabled.return_value = {
            'runtime_mode': 'standby',
            'system_proxy_active': False,
        }
        config = routing.normalize_config({
            'enabled': True,
            'capture_mode': 'system-proxy',
            'mixed_port': 17890,
        })

        with self.assertRaisesRegex(routing.RoutingError, '状态回读不一致'):
            manager._fast_toggle_system_proxy(config, True)
        self.assertEqual(
            manager._native_service.set_system_proxy_enabled.call_args_list,
            [mock.call(True), mock.call(False)])

    def test_fast_enable_waits_for_registry_notification(self):
        manager = self._manager()
        manager._native_service.set_system_proxy_enabled.return_value = {
            'runtime_mode': 'active',
            'system_proxy_active': True,
        }
        config = routing.normalize_config({
            'enabled': True,
            'capture_mode': 'system-proxy',
            'mixed_port': 17890,
        })
        with mock.patch.object(
                routing, 'windows_system_proxy',
                side_effect=['', 'http://127.0.0.1:17890']), \
                mock.patch.object(routing.proxy_guard, 'set_proxy_enabled') as enable:
            result = manager._fast_toggle_system_proxy(config, True)

        self.assertTrue(result['ok'])
        enable.assert_called_once_with(True)
        manager._native_service.set_system_proxy_enabled.assert_called_once_with(True)

    def test_fast_disable_refreshes_current_user_windows_settings(self):
        manager = self._manager()
        manager._native_service.set_system_proxy_enabled.return_value = {
            'runtime_mode': 'standby',
            'system_proxy_active': False,
        }
        manager.status = mock.Mock(return_value={'running': False})
        config = routing.normalize_config({
            'enabled': False,
            'capture_mode': 'system-proxy',
            'mixed_port': 17890,
        })
        with mock.patch.object(
                routing.proxy_guard, 'refresh_user_proxy_settings',
                return_value={'ok': True}) as refresh:
            result = manager._fast_toggle_system_proxy(config, False)

        self.assertTrue(result['ok'])
        refresh.assert_called_once_with(timeout_ms=1000)

    @staticmethod
    def _api_for_startup_reconcile(enabled):
        api = Api.__new__(Api)
        api._routing_standby_thread = None
        api._routing_lock = threading.Lock()
        api._cfg_get = mock.Mock(return_value={
            'routing': routing.normalize_config({
                'enabled': enabled,
                'capture_mode': 'system-proxy',
                'mixed_port': 17890,
                'proxy_providers': [{
                    'id': 'alpha', 'name': '订阅一',
                    'url': 'https://example.test/sub', 'enabled': True,
                }],
            }),
        })
        api.log = mock.Mock()
        api.routing = mock.Mock()
        api.routing._service_state.return_value = {
            'installed': True, 'backend': 'native',
            'runtime_running': True, 'runtime_mode': 'standby',
            'fast_toggle_ready': True,
        }
        api.routing._can_fast_toggle_system_proxy.return_value = True
        api.routing._fast_toggle_system_proxy.return_value = {'ok': True}
        return api

    def test_startup_restores_persisted_enabled_proxy_state(self):
        api = self._api_for_startup_reconcile(True)
        with mock.patch('api.routing.windows_system_proxy', return_value=''):
            api._start_routing_standby_reconcile()
            api._routing_standby_thread.join(1)
        api.routing._fast_toggle_system_proxy.assert_called_once_with(
            mock.ANY, True)

    def test_startup_preserves_active_foreign_system_proxy(self):
        api = self._api_for_startup_reconcile(True)
        with mock.patch(
                'api.routing.windows_system_proxy',
                return_value='http://127.0.0.1:7897'):
            api._start_routing_standby_reconcile()
            api._routing_standby_thread.join(1)

        api.routing._fast_toggle_system_proxy.assert_not_called()
        api.routing.apply.assert_not_called()
        self.assertTrue(any(
            '保留当前代理并跳过 CXVPN 自动接管' in call.args[0]
            for call in api.log.call_args_list))

    def test_startup_clears_only_our_stale_proxy_when_persisted_disabled(self):
        api = self._api_for_startup_reconcile(False)
        api.routing._can_fast_toggle_system_proxy.return_value = False
        with mock.patch(
                'api.routing.windows_system_proxy',
                return_value='http://127.0.0.1:17890'), \
                mock.patch('api.proxy_guard.set_proxy_enabled') as disable:
            api._start_routing_standby_reconcile()
            api._routing_standby_thread.join(1)
        disable.assert_called_once_with(False)
        api.routing._fast_toggle_system_proxy.assert_not_called()

    def test_startup_guard_repairs_windows_without_changing_codex(self):
        api = self._api_for_startup_reconcile(False)
        api.worker = mock.Mock()
        api.worker.is_alive.return_value = False
        api.routing_updates = mock.Mock()
        api.routing_network = mock.Mock()
        api._start_routing_standby_reconcile = mock.Mock()
        with mock.patch('api.proxy_guard.startup_check', return_value='127.0.0.1:17890'):
            api.start()

        api.worker.start.assert_called_once_with()
        api._start_routing_standby_reconcile.assert_called_once_with()
        self.assertEqual(api._pending_notice['title'], '系统代理已修复')

    def test_startup_with_active_system_proxy_or_tun_leaves_codex_alone(self):
        for mode in ('system-proxy', 'tun'):
            with self.subTest(mode=mode):
                api = self._api_for_startup_reconcile(True)
                api._cfg_get.return_value['routing']['capture_mode'] = mode
                api.routing = self._manager()
                api.routing._service_state = mock.Mock(return_value={
                    'installed': True, 'backend': 'native',
                    'runtime_running': True, 'runtime_mode': 'active',
                })
                api.routing.apply = mock.Mock()
                with mock.patch.object(
                        routing, 'windows_system_proxy',
                        return_value='http://127.0.0.1:17890' if mode == 'system-proxy' else ''):
                    api._start_routing_standby_reconcile()
                    api._routing_standby_thread.join(1)
                self.assertFalse(api._routing_standby_thread.is_alive())
                api.routing.apply.assert_not_called()
                self.assertFalse(any(
                    '复核失败' in call.args[0] for call in api.log.call_args_list))

    def test_service_commits_and_port_changes_leave_codex_alone(self):
        for mode in ('system-proxy', 'tun'):
            manager = self._manager()
            manager._native_service.apply.return_value = {'transaction_id': 'tx'}
            manager._native_provider_files = mock.Mock(return_value=[])
            manager._wait_native_ready = mock.Mock()
            manager._refresh_user_proxy_settings = mock.Mock(return_value=True)
            for port in (17890, 19000):
                with self.subTest(mode=mode, port=port):
                    config = routing.normalize_config({
                        'enabled': True, 'capture_mode': mode, 'mixed_port': port,
                    })
                    with mock.patch.object(
                            routing, 'windows_system_proxy',
                            return_value=f'http://127.0.0.1:{port}'), \
                            mock.patch.object(routing, 'windows_manual_proxy_state',
                                              return_value={'enabled': False, 'server': ''}):
                        manager._install('offline-candidate.json', config)
            self.assertEqual(manager._native_service.commit.call_count, 2)
            manager._native_service.rollback.assert_not_called()

    def test_full_service_stop_leaves_codex_alone(self):
        manager = self._manager()
        manager._service_state = mock.Mock(return_value={
            'installed': True, 'backend': 'native', 'runtime_mode': 'active',
        })
        config = routing.normalize_config({'enabled': False, 'proxy_providers': []})
        with mock.patch.object(
                routing.proxy_guard, 'refresh_user_proxy_settings',
                return_value={'ok': True}) as refresh:
            result = manager.apply(config)
        self.assertTrue(result['ok'])
        manager._native_service.stop_runtime.assert_called_once_with()
        refresh.assert_called_once_with(timeout_ms=1000)


if __name__ == '__main__':
    unittest.main()
