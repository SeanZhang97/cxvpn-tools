# -*- coding: utf-8 -*-
"""代理启停边界回归；所有服务、代理与核心操作均为模拟。"""
import unittest
from unittest import mock

from core import routing


def config():
    return routing.normalize_config({
        'enabled': False, 'capture_mode': 'tun', 'controller_secret': 'offline-secret',
        'proxy_providers': [{'id': 'alpha', 'name': '订阅 e\u0301 🇨🇳',
                             'url': 'https://example.test/sub', 'enabled': True}],
    })


class RoutingLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.manager = routing.RoutingManager()
        self.manager._native_service = mock.Mock()
        self.manager.status = mock.Mock(return_value={'running': False})
        self.manager._controller_version = mock.Mock(return_value='offline-version')
        self.manager._start_standby_runtime = mock.Mock()
        self.manager._repair_invalid_proxy = mock.Mock()
        self.manager._confirm_disabled_proxy = mock.Mock()
        self.value = config()
        self.state = {
            'installed': True, 'state': 'Running', 'backend': 'native',
            'service_compatible': True, 'runtime_running': True,
            'runtime_enabled': True, 'runtime_mode': 'standby',
            'system_proxy_active': False,
            'pending_transaction': None, 'crash_fused': False,
            'applied_config_signature': self.manager._config_signature(self.value),
        }
        self.manager._service_state = mock.Mock(side_effect=lambda **kw: self.state)

    def test_unchanged_disabled_config_reuses_confirmed_standby(self):
        result = self.manager.apply(self.value, defer_standby=True)
        self.assertTrue(result['ok'])
        self.assertFalse(result['standby_pending'])
        self.manager._native_service.stop_runtime.assert_not_called()
        self.manager._start_standby_runtime.assert_not_called()
        self.manager._confirm_disabled_proxy.assert_not_called()

    def test_saved_config_equality_cannot_reuse_mismatched_runtime(self):
        self.state['applied_config_signature'] = 'different-config'
        self.manager.apply(self.value)
        self.manager._native_service.stop_runtime.assert_called_once()
        self.manager._start_standby_runtime.assert_called_once_with(self.value)

    def test_unhealthy_pending_or_old_standby_is_not_reused(self):
        for update in ({'pending_transaction': 'tx'}, {'service_compatible': False},
                       {'crash_fused': True}, {'runtime_mode': 'active'},
                       {'system_proxy_active': True}):
            with self.subTest(update=update):
                original = dict(self.state)
                self.state.update(update)
                self.manager._native_service.reset_mock()
                self.manager.apply(self.value, defer_standby=True)
                self.manager._native_service.stop_runtime.assert_called_once()
                self.state = original

    def test_unresponsive_controller_rebuilds_matching_standby(self):
        self.manager._controller_version.return_value = ''
        self.manager.apply(self.value)
        self.manager._native_service.stop_runtime.assert_called_once()
        self.manager._start_standby_runtime.assert_called_once()

    def test_stopped_core_still_retries_native_cleanup(self):
        self.state.update(runtime_running=False, runtime_enabled=False, runtime_mode='stopped')
        self.manager.apply(self.value)
        self.manager._native_service.stop_runtime.assert_called_once()
        self.manager._confirm_disabled_proxy.assert_called_once()
        self.manager._start_standby_runtime.assert_called_once_with(self.value)

    def test_failed_cleanup_does_not_start_standby_or_report_disabled(self):
        self.state.update(runtime_running=False, runtime_enabled=False, runtime_mode='stopped')
        self.manager._native_service.stop_runtime.side_effect = (
            routing._routing_service.ServiceError('snapshot restore failed'))
        with self.assertRaisesRegex(routing.RoutingError, '关闭统一分流失败'):
            self.manager.apply(self.value)
        self.manager._start_standby_runtime.assert_not_called()

    def test_marked_for_restart_is_not_treated_as_stopped(self):
        self.state.update(runtime_running=False, runtime_enabled=True, runtime_mode='stopped')
        self.manager.apply(self.value)
        self.manager._native_service.stop_runtime.assert_called_once()

    def test_exited_candidate_fails_before_controller_timeout(self):
        self.manager._native_service.status.return_value = {'runtime_running': False}
        with mock.patch.object(routing.time, 'sleep') as sleep:
            with self.assertRaisesRegex(routing.RoutingError, '启动期间已退出'):
                self.manager._wait_native_ready(self.value)
        self.manager._controller_version.assert_not_called()
        sleep.assert_not_called()

    def test_ready_standby_does_not_probe_public_egress(self):
        self.manager._native_service.status.return_value = {'runtime_running': True}
        self.manager._verify_runtime_egress = mock.Mock()
        self.manager._wait_native_ready(self.value, 'standby')
        self.manager._verify_runtime_egress.assert_not_called()

    def test_fast_toggle_requires_compatible_nonpending_service(self):
        value = {**self.value, 'capture_mode': 'system-proxy'}
        state = {**self.state, 'fast_toggle_ready': True}
        self.assertTrue(self.manager._can_fast_toggle_system_proxy(value, state))
        for update in ({'service_compatible': False}, {'pending_transaction': 'tx'}):
            self.assertFalse(self.manager._can_fast_toggle_system_proxy(value, {**state, **update}))

    def test_fast_enable_does_not_enable_foreign_endpoint(self):
        value = routing.normalize_config({'enabled': True, 'capture_mode': 'system-proxy'})
        self.manager._verify_runtime_egress = mock.Mock()
        self.manager._native_service.set_system_proxy_enabled.side_effect = [
            {'runtime_mode': 'active', 'system_proxy_active': True},
            {'runtime_mode': 'standby', 'system_proxy_active': False},
        ]
        with mock.patch.object(routing, 'windows_system_proxy', return_value='http://127.0.0.1:7897'), \
                mock.patch.object(routing.proxy_guard, 'read_proxy_state',
                                  return_value={'enable': False, 'server': '127.0.0.1:7897'}), \
                mock.patch.object(routing.proxy_guard, 'set_proxy_enabled') as enable, \
                mock.patch.object(routing.time, 'monotonic', side_effect=[0, 0, 2]):
            with self.assertRaisesRegex(routing.RoutingError, '回读不一致'):
                self.manager._fast_toggle_system_proxy(value, True)
        enable.assert_not_called()

    def test_fast_toggle_rollback_failure_is_not_reported_as_restored(self):
        self.manager._native_service.set_system_proxy_enabled.side_effect = (
            routing._routing_service.ServiceError('offline rollback rejected'))
        with self.assertRaisesRegex(routing.RoutingError, '恢复失败') as caught:
            self.manager._fail_fast_toggle('代理开启后回读不一致')
        self.assertNotIn('已自动恢复', str(caught.exception))


class InvalidProxyReconcileTests(unittest.TestCase):
    def test_legal_proxy_is_preserved(self):
        manager = routing.RoutingManager()
        with mock.patch.object(routing.proxy_guard, 'read_proxy_state',
                               return_value={'enable': True, 'server': 'proxy.example:8080'}), \
                mock.patch.object(routing.proxy_guard, 'repair_invalid_manual_proxy') as repair:
            manager._repair_invalid_proxy('关闭后')
        repair.assert_not_called()

    def test_repair_is_followed_by_refresh_and_readback(self):
        manager = routing.RoutingManager()
        manager._refresh_user_proxy_settings = mock.Mock(return_value=True)
        with mock.patch.object(routing.proxy_guard, 'read_proxy_state', side_effect=[
                {'enable': True, 'server': ':'}, {'enable': False, 'server': ''}]), \
                mock.patch.object(routing.proxy_guard, 'repair_invalid_manual_proxy', return_value=True):
            manager._repair_invalid_proxy('关闭后')
        manager._refresh_user_proxy_settings.assert_called_once()

    def test_reappearing_invalid_state_is_not_reported_as_success(self):
        manager = routing.RoutingManager()
        manager._refresh_user_proxy_settings = mock.Mock(return_value=True)
        with mock.patch.object(routing.proxy_guard, 'read_proxy_state',
                               return_value={'enable': True, 'server': ':'}), \
                mock.patch.object(routing.proxy_guard, 'repair_invalid_manual_proxy', return_value=True):
            with self.assertRaisesRegex(routing.RoutingError, '无法确认'):
                manager._repair_invalid_proxy('关闭后')


if __name__ == '__main__':
    unittest.main()
