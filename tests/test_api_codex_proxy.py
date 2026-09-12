# -*- coding: utf-8 -*-
"""api.py 中 Codex 交互接口的离线测试：不访问真实 Codex、网络、服务或注册表。"""
import unittest
from unittest import mock

import api

from core import codex_proxy


class ApiCodexProxyTests(unittest.TestCase):
    def instance(self, routing=None):
        target = api.Api.__new__(api.Api)
        target.routing = mock.Mock()
        target._cfg_get = mock.Mock(return_value={
            'routing': routing or {'mixed_port': 19000},
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
                    'deviated_fields': ['features.respect_system_proxy']}):
            state = target.get_codex_status()

        self.assertTrue(state['ok'])
        self.assertEqual('C:/u/.codex/config.toml', state['config_path'])
        self.assertTrue(state['snapshot_exists'])
        self.assertEqual(3, state['snapshot_fields'])
        self.assertEqual(17890, state['last_mixed_port'])
        self.assertEqual(18080, state['mixed_port'])
        self.assertEqual(
            ['features.respect_system_proxy'], state['deviated_fields'])


if __name__ == '__main__':
    unittest.main()