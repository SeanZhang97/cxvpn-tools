# -*- coding: utf-8 -*-
import unittest
from unittest import mock

from core import routing
from core.mihomo_activity import (
    MAX_CLOSED_CONNECTIONS, MihomoActivityRelay,
    _sanitize_connection, _sanitize_log_payload,
)


class MihomoActivityTests(unittest.TestCase):
    def setUp(self):
        self.manager = mock.Mock()
        self.relay = MihomoActivityRelay(
            lambda: {'routing': {'controller_secret': 'secret-token'}},
            self.manager, mock.Mock())

    def test_connection_snapshot_keeps_useful_fields_and_hides_process_path(self):
        safe = _sanitize_connection({
            'id': '连接-🇯🇵',
            'metadata': {
                'host': 'example.com', 'destinationIP': '203.0.113.7',
                'destinationPort': '443', 'process': '浏览器 e\u0301',
                'processPath': r'C:\Users\tester\AppData\browser.exe',
            },
            'upload': 12, 'download': 34, 'chains': ['节点-日本'],
            'rule': 'DomainSuffix', 'rulePayload': 'example.com',
        })
        self.assertEqual(safe['id'], '连接-🇯🇵')
        self.assertEqual(safe['metadata']['host'], 'example.com')
        self.assertEqual(safe['metadata']['process_path'], 'browser.exe')
        self.assertNotIn('Users', safe['metadata']['process_path'])

    def test_core_log_redacts_known_secrets_credentials_and_query_tokens(self):
        value = _sanitize_log_payload(
            'open https://user:pass@example.com/sub?token=abc&x=1 '
            'secret-token Authorization: Bearer another-secret',
            ('secret-token',))
        self.assertNotIn('user:pass', value)
        self.assertNotIn('abc', value)
        self.assertNotIn('secret-token', value)
        self.assertNotIn('another-secret', value)
        self.assertIn('[redacted]', value)

    def test_connection_diff_moves_removed_items_to_bounded_history(self):
        self.relay._apply_connections({'connections': [
            {'id': str(index), 'metadata': {'host': f'{index}.example'}}
            for index in range(MAX_CLOSED_CONNECTIONS + 10)
        ]})
        self.relay._apply_connections({'connections': []})
        snapshot = self.relay.snapshot('connections')['snapshot']
        self.assertEqual(snapshot['active'], [])
        self.assertEqual(len(snapshot['closed']), MAX_CLOSED_CONNECTIONS)

    def test_wait_returns_only_after_version_changes(self):
        version = self.relay.snapshot('logs')['version']
        self.relay._apply_log(
            {'type': 'warn', 'payload': '中文 e\u0301 🇯🇵'},
            {'controller_secret': 'not-present'})
        result = self.relay.wait(version, 'logs', 1)
        self.assertTrue(result['changed'])
        self.assertEqual(result['snapshot']['items'][0]['type'], 'warning')

    def test_close_connection_uses_routing_manager_public_api(self):
        self.manager.close_connections.return_value = {
            'ok': True, 'closed': 'one'}
        result = self.relay.close_connection('id with/slash')
        self.assertTrue(result['ok'])
        self.manager.close_connections.assert_called_once_with(
            {'controller_secret': 'secret-token'}, 'id with/slash')

    def test_routing_manager_quotes_connection_id(self):
        manager = routing.RoutingManager()
        config = routing.default_config()
        with mock.patch.object(
                manager, '_controller_request', return_value={}) as request:
            result = manager.close_connections(config, 'id with/slash')
        self.assertTrue(result['ok'])
        self.assertEqual(request.call_args.args[1], '/connections/id%20with%2Fslash')
        self.assertEqual(request.call_args.kwargs['method'], 'DELETE')


if __name__ == '__main__':
    unittest.main()
