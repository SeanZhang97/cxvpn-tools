# -*- coding: utf-8 -*-
"""api.py 中 Codex 交互接口的离线测试：不访问真实 Codex、网络、服务或注册表。"""
import threading
import unittest
from unittest import mock

import api

from core import codex_proxy, codex_runtime, routing


class ApiCodexProxyTests(unittest.TestCase):
    def instance(self, routing=None):
        target = api.Api.__new__(api.Api)
        target.routing = mock.Mock()
        target.cfg = {
            'routing': routing or {'mixed_port': 19000},
            'codex_api': {'base_url': '', 'api_key': ''},
        }
        target._lock = threading.Lock()
        target._cfg_get = mock.Mock(side_effect=lambda: {
            key: (dict(value) if isinstance(value, dict) else value)
            for key, value in target.cfg.items()
        })
        target.log = mock.Mock()
        target._routing_stream_state = ''
        target._routing_lock = __import__('threading').Lock()
        target.routing_activity = mock.Mock()
        return target

    def test_sync_reads_port_from_config_not_argument(self):
        target = self.instance({'mixed_port': 19000})
        with mock.patch.object(
                codex_proxy, 'sync', return_value={'ok': True, 'changed': True}) as sync:
            result = target.sync_codex_proxy()

        self.assertTrue(result['ok'])
        self.assertTrue(result['restart_required_after_change'])
        sync.assert_called_once_with(19000, logger=target.log)
        logged = ' '.join(call.args[0] for call in target.log.call_args_list)
        self.assertIn('19000', logged)

    def test_sync_failure_returns_warning_without_restart_flag(self):
        target = self.instance({'mixed_port': 17890})
        with mock.patch.object(
                codex_proxy, 'sync',
                return_value={'ok': False, 'warning': '写入失败'}) as sync:
            result = target.sync_codex_proxy()

        self.assertFalse(result['ok'])
        self.assertEqual('写入失败', result['warning'])
        self.assertNotIn('restart_required_after_change', result)
        sync.assert_called_once_with(17890, logger=target.log)

    def test_restore_marks_restart_only_when_changed(self):
        target = self.instance()
        with mock.patch.object(
                codex_proxy, 'restore',
                return_value={'ok': True, 'changed': True}) as restore:
            result = target.restore_codex_proxy()

        self.assertTrue(result['ok'])
        self.assertTrue(result['restart_required_after_change'])
        restore.assert_called_once_with(logger=target.log)

        with mock.patch.object(
                codex_proxy, 'restore',
                return_value={'ok': True, 'changed': False}) as restore:
            result = target.restore_codex_proxy()

        self.assertTrue(result['ok'])
        self.assertNotIn('restart_required_after_change', result)

    def test_read_config_returns_text_but_log_keeps_no_content(self):
        target = self.instance()
        secret = 'TOKEN = "private-value-🇯🇵"'
        with mock.patch.object(
                codex_proxy, 'read_config',
                return_value={'ok': True, 'exists': True, 'text': secret,
                              'path': 'C:/x/config.toml', 'bytes': 10}) as read:
            result = target.read_codex_config()

        self.assertTrue(result['ok'])
        self.assertEqual(secret, result['text'])
        read.assert_called_once_with(logger=target.log)
        logged = ' '.join(call.args[0] for call in target.log.call_args_list)
        self.assertNotIn('private-value', logged)
        self.assertNotIn('TOKEN', logged)

    def test_read_config_missing_file_passes_through(self):
        target = self.instance()
        with mock.patch.object(
                codex_proxy, 'read_config',
                return_value={'ok': True, 'exists': False, 'text': '',
                              'path': 'C:/x/config.toml', 'bytes': 0}):
            result = target.read_codex_config()

        self.assertTrue(result['ok'])
        self.assertFalse(result['exists'])

    def test_get_codex_status_includes_snapshot_summary(self):
        target = self.instance({'mixed_port': 18080, 'capture_mode': 'system-proxy'})
        with mock.patch.object(
                codex_proxy, 'status',
                return_value={
                    'ok': True, 'path': 'C:/u/.codex/config.toml',
                    'config_exists': True, 'parse_error': '',
                    'snapshot_exists': True, 'snapshot_fields': 3,
                    'last_mixed_port': 17890, 'synced_fields': 3,
                    'deviated_fields': ['features.respect_system_proxy']}), \
                mock.patch.object(
                    codex_proxy, 'transport_status',
                    return_value={
                        'transport_mode': 'http_only',
                        'websocket_enabled': False,
                        'transport_managed': True,
                        'transport_restore_available': True,
                        'transport_cleanup_pending': False,
                        'active_provider': codex_proxy.HTTP_PROVIDER_ID}), \
                mock.patch.object(
                    codex_proxy, 'custom_api_status',
                    return_value={
                         'custom_api_configured': True,
                         'custom_api_active': True,
                        'custom_api_managed': True,
                         'custom_api_migration_required': False,
                         'custom_api_websocket_enabled': False,
                        'custom_api_base_url': 'https://api.example.test/v1',
                        'custom_api_key_configured': True,
                        'custom_api_openai_auth_required': True}):
            state = target.get_codex_status()

        self.assertTrue(state['ok'])
        self.assertEqual('C:/u/.codex/config.toml', state['config_path'])
        self.assertTrue(state['snapshot_exists'])
        self.assertEqual(3, state['snapshot_fields'])
        self.assertEqual(17890, state['last_mixed_port'])
        self.assertEqual(18080, state['mixed_port'])
        self.assertEqual(
            ['features.respect_system_proxy'], state['deviated_fields'])
        self.assertEqual('http_only', state['transport_mode'])
        self.assertIs(False, state['websocket_enabled'])
        self.assertTrue(state['transport_managed'])
        self.assertTrue(state['transport_restore_available'])
        self.assertFalse(state['transport_cleanup_pending'])
        self.assertTrue(state['custom_api_managed'])
        self.assertFalse(state['custom_api_migration_required'])
        self.assertIs(False, state['custom_api_websocket_enabled'])
        self.assertTrue(state['custom_api_key_configured'])
        self.assertFalse(state['custom_api_key_stored'])
        self.assertTrue(state['custom_api_openai_auth_required'])
        self.assertNotIn('api_key', state)

    def test_set_codex_websocket_requires_strict_boolean(self):
        target = self.instance()
        with mock.patch.object(codex_proxy, 'set_websocket_enabled') as setter:
            for value in ('false', 0, None):
                with self.subTest(value=value):
                    result = target.set_codex_websocket(value)
                    self.assertFalse(result['ok'])
            setter.assert_not_called()

    def test_set_codex_websocket_passes_explicit_target_and_marks_restart(self):
        target = self.instance()
        with mock.patch.object(
                codex_proxy, 'set_websocket_enabled',
                return_value={'ok': True, 'changed': True,
                              'transport_mode': 'http_only'}) as setter:
            result = target.set_codex_websocket(False)

        self.assertTrue(result['ok'])
        self.assertTrue(result['restart_required_after_change'])
        setter.assert_called_once_with(False, logger=target.log)
        logged = ' '.join(call.args[0] for call in target.log.call_args_list)
        self.assertIn('请求已提交', logged)
        self.assertIn('开始执行', logged)
        self.assertIn('执行完成', logged)

    def test_custom_api_save_and_restore_are_logged_without_secrets(self):
        target = self.instance()
        secret = 'private-test-key'
        base_url = 'http://192.0.2.20:53142/v1'
        with mock.patch.object(
                codex_proxy, 'save_custom_api',
                return_value={'ok': True, 'changed': True,
                              'base_url': base_url, 'key_configured': True}) as save, \
                mock.patch.object(api.cfgmod, 'save') as persist:
            result = target.save_codex_api_config(base_url, secret)

        self.assertTrue(result['restart_required_after_change'])
        self.assertTrue(result['key_stored'])
        save.assert_called_once_with(base_url, secret, logger=target.log)
        persist.assert_called_once()
        self.assertEqual(secret, target.cfg['codex_api']['api_key'])
        logged = ' '.join(call.args[0] for call in target.log.call_args_list)
        self.assertNotIn(secret, logged)
        self.assertNotIn(base_url, logged)
        self.assertIn('请求已提交', logged)
        self.assertIn('开始执行', logged)
        self.assertIn('执行完成', logged)

        target.log.reset_mock()
        with mock.patch.object(
                codex_proxy, 'restore_custom_api',
                return_value={'ok': True, 'changed': True, 'warnings': []}) as restore:
            restored = target.restore_codex_api_config()
        self.assertTrue(restored['restart_required_after_change'])
        restore.assert_called_once_with(logger=target.log)
        self.assertEqual(secret, target.cfg['codex_api']['api_key'])

    def test_custom_api_key_echo_is_local_and_not_logged(self):
        target = self.instance()
        secret = 'private-echo-key'
        target.cfg['codex_api'] = {
            'base_url': 'https://api.example.test/v1',
            'api_key': secret,
        }
        with mock.patch.object(
                codex_proxy, 'read_custom_api_key') as reader:
            result = target.read_codex_api_key()

        self.assertEqual(secret, result['api_key'])
        self.assertTrue(result['key_stored'])
        reader.assert_not_called()
        logged = ' '.join(call.args[0] for call in target.log.call_args_list)
        self.assertNotIn(secret, logged)
        self.assertIn('请求已提交', logged)
        self.assertIn('开始执行', logged)
        self.assertIn('执行完成', logged)

    def test_custom_api_key_migrates_from_codex_into_local_config(self):
        target = self.instance()
        secret = 'migrated-private-key'
        base_url = 'https://api.example.test/v1'
        with mock.patch.object(
                codex_proxy, 'read_custom_api_key',
                return_value={'ok': True, 'key_configured': True,
                              'api_key': secret}) as reader, \
                mock.patch.object(
                    codex_proxy, 'custom_api_status',
                    return_value={'custom_api_base_url': base_url}), \
                mock.patch.object(api.cfgmod, 'save') as persist:
            result = target.read_codex_api_key()

        self.assertTrue(result['ok'])
        self.assertTrue(result['key_stored'])
        self.assertEqual(secret, target.cfg['codex_api']['api_key'])
        self.assertEqual(base_url, target.cfg['codex_api']['base_url'])
        reader.assert_called_once_with(logger=target.log)
        persist.assert_called_once()

    def test_custom_api_save_reuses_key_stored_in_sqlite(self):
        target = self.instance()
        secret = 'stored-private-key'
        base_url = 'https://api.example.test/v1'
        target.cfg['codex_api'] = {
            'base_url': base_url,
            'api_key': secret,
        }
        with mock.patch.object(
                codex_proxy, 'save_custom_api',
                return_value={'ok': True, 'changed': False,
                              'base_url': base_url}) as save, \
                mock.patch.object(api.cfgmod, 'save'):
            result = target.save_codex_api_config(base_url, '')

        self.assertTrue(result['ok'])
        self.assertTrue(result['key_stored'])
        save.assert_called_once_with(base_url, secret, logger=target.log)

    def test_restart_codex_delegates_and_logs_lifecycle(self):
        target = self.instance()
        with mock.patch.object(
                codex_runtime, 'restart_codex',
                return_value={'ok': True, 'msg': 'Codex 已重新启动'}) as restart:
            result = target.restart_codex()

        self.assertTrue(result['ok'])
        restart.assert_called_once_with(logger=target.log)
        logged = ' '.join(call.args[0] for call in target.log.call_args_list)
        self.assertIn('请求已提交', logged)
        self.assertIn('开始执行', logged)
        self.assertIn('执行完成', logged)

    def test_status_and_view_never_sync_or_restore_when_routing_enabled(self):
        target = self.instance({'enabled': True, 'mixed_port': 19000})
        with mock.patch.object(codex_proxy, 'sync') as sync, \
                mock.patch.object(codex_proxy, 'restore') as restore, \
                mock.patch.object(codex_proxy, 'status', return_value={
                    'config_exists': True, 'path': 'offline/config.toml',
                    'parse_error': '', 'snapshot_exists': True, 'snapshot_fields': 17,
                    'last_mixed_port': 17890, 'synced_fields': 17, 'deviated_fields': [],
                }), \
                mock.patch.object(codex_proxy, 'transport_status', return_value={
                    'transport_mode': 'wss_preferred', 'websocket_enabled': True,
                    'active_provider': 'openai'}), \
                mock.patch.object(codex_proxy, 'custom_api_status', return_value={}), \
                mock.patch.object(codex_proxy, 'read_config', return_value={'ok': True}):
            for _ in range(2):
                state = target.get_codex_status()
                self.assertTrue(target.read_codex_config()['ok'])
                self.assertEqual(state['mixed_port'], 19000)
                self.assertEqual(state['last_mixed_port'], 17890)
            sync.assert_not_called()
            restore.assert_not_called()


class ApiCodexProxyCloseTests(unittest.TestCase):
    def setUp(self):
        for target, name, options in (
                ('api.cfgmod.save', 'save', {}),
                ('api.routing.subscription_store.prune_cache', 'prune', {}),
                ('api.codex_proxy.sync', 'sync', {}),
                ('api.codex_proxy.restore', 'restore', {
                    'return_value': {'ok': True, 'changed': True}})):
            patcher = mock.patch(target, **options)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.sync.assert_not_called()

    def instance(self, enabled=True):
        target = api.Api.__new__(api.Api)
        target.cfg = {'routing': routing.normalize_config({
            'enabled': enabled, 'mixed_port': 19000})}
        target._lock = threading.Lock()
        target._routing_lock = threading.Lock()
        target.log = mock.Mock()
        target.routing = mock.Mock()
        target._poke_ui_state = mock.Mock()
        target._start_routing_standby_reconcile = mock.Mock()
        target.routing.apply.side_effect = lambda value, **_: {
            'ok': True, 'config': value, 'warnings': [], 'msg': '配置已应用'}
        return target

    def test_disable_from_page_home_and_tray_cleans_only_after_commit(self):
        for entry in ('page', 'home', 'tray'):
            with self.subTest(entry=entry):
                target = self.instance()
                self.save.reset_mock()
                self.restore.reset_mock()
                self.save.side_effect = lambda _: self.restore.assert_not_called()

                def restore(**_):
                    self.save.assert_called_once()
                    self.assertFalse(target.cfg['routing']['enabled'])
                    return {'ok': True, 'changed': True}

                self.restore.side_effect = restore
                if entry == 'page':
                    result = target.apply_routing({**target.cfg['routing'], 'enabled': False})
                elif entry == 'home':
                    result = target.set_routing_enabled(False)
                else:
                    result = target.desktop_toggle_routing()
                self.assertTrue(result['ok'])
                self.assertIn('需重启 Codex', result['msg'])
                self.restore.assert_called_once_with(logger=target.log)
                target.routing.apply.assert_called_once()

    def test_enable_port_change_and_already_disabled_save_leave_codex_alone(self):
        for previous, requested, port in ((False, True, 19000),
                                          (True, True, 19100),
                                          (False, False, 19000)):
            with self.subTest(previous=previous, requested=requested, port=port):
                target = self.instance(previous)
                result = target.apply_routing({
                    **target.cfg['routing'], 'enabled': requested, 'mixed_port': port})
                self.assertTrue(result['ok'])
                self.restore.assert_not_called()

    def test_failed_disable_or_config_rollback_never_cleans_codex(self):
        for stage in ('service', 'save'):
            with self.subTest(stage=stage):
                target = self.instance()
                if stage == 'service':
                    target.routing.apply.side_effect = routing.RoutingError('offline failure')
                else:
                    self.save.side_effect = OSError('offline save failure')
                result = target.set_routing_enabled(False)
                self.assertFalse(result['ok'])
                self.assertTrue(target.cfg['routing']['enabled'])
                self.assertEqual(target.routing.apply.call_count, 1 if stage == 'service' else 2)
                self.restore.assert_not_called()

    def test_cleanup_failure_or_manual_conflict_preserves_successful_proxy_stop(self):
        for cleanup in ({'ok': False, 'warning': 'offline failure'},
                        OSError('offline interruption'),
                        {'ok': True, 'changed': True, 'warnings': ['features.respect_system_proxy']}):
            with self.subTest(cleanup=type(cleanup).__name__):
                target = self.instance()
                self.restore.side_effect = cleanup if isinstance(cleanup, Exception) else None
                self.restore.return_value = cleanup
                result = target.set_routing_enabled(False)
                self.assertTrue(result['ok'])
                self.assertFalse(target.cfg['routing']['enabled'])
                self.assertTrue(result['warnings'])
                target.routing.apply.assert_called_once()


if __name__ == '__main__':
    unittest.main()
