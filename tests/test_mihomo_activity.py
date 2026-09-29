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

    def test_confirmed_core_stop_retires_connections_once(self):
        self.relay._apply_connections({'connections': [
            {'id': 'old-vpn', 'chains': ['VPN-1']},
        ]})
        self.manager.status.return_value = {
            'ok': True, 'running': False, 'core_running': False,
            'service_state': 'Running', 'runtime_mode': 'stopped',
        }
        self.relay.set_view('connections')
        with mock.patch.object(
                self.relay, '_wait_lifecycle',
                side_effect=lambda *_: self.relay._stop.set()):
            self.relay._run()
        snapshot = self.relay.snapshot('connections')['snapshot']
        self.assertEqual(snapshot['phase'], 'idle')
        self.assertEqual(snapshot['active'], [])
        self.assertEqual([item['id'] for item in snapshot['closed']], ['old-vpn'])
        self.relay._retire_connections()
        self.assertEqual(len(self.relay.snapshot('connections')['snapshot']['closed']), 1)

    def test_core_restart_retires_old_snapshot_before_new_stream(self):
        self.manager.status.side_effect = [
            {'core_running': True, 'mihomo_pid': 100},
            {'core_running': True, 'mihomo_pid': 200},
        ]
        self.relay.set_view('connections')
        observed = []

        def stream(*_):
            observed.append(self.relay.snapshot('connections')['snapshot'])
            if len(observed) == 1:
                self.relay._apply_connections({'connections': [{'id': 'old-pid'}]})
            else:
                self.relay._stop.set()

        with mock.patch.object(self.relay, '_stream', side_effect=stream), \
                mock.patch.object(self.relay, '_wait_lifecycle'):
            self.relay._run()
        self.assertEqual(observed[1]['active'], [])
        self.assertEqual([item['id'] for item in observed[1]['closed']], ['old-pid'])

    def test_unknown_core_and_temporary_stream_failure_keep_active_snapshot(self):
        for status in (
                {'ok': True, 'service_state': 'Unknown', 'core_running': False},
                {'ok': False, 'core_running': False},
                {'ok': True, 'core_running': True, 'mihomo_pid': 100}):
            with self.subTest(status=status):
                self.setUp()
                self.relay._apply_connections({'connections': [{'id': 'possibly-live'}]})
                self.manager.status.return_value = status
                self.relay.set_view('connections')
                with mock.patch.object(
                        self.relay, '_stream', side_effect=ConnectionError('transient')), \
                        mock.patch.object(self.relay, '_wait_lifecycle',
                                          side_effect=lambda *_: self.relay._stop.set()), \
                        mock.patch.object(self.relay, '_set_phase',
                                          wraps=self.relay._set_phase) as phase:
                    self.relay._run()
                snapshot = self.relay.snapshot('connections')['snapshot']
                self.assertEqual([item['id'] for item in snapshot['active']], ['possibly-live'])
                self.assertEqual(snapshot['closed'], [])
                phase.assert_any_call('connections', 'reconnecting', 1)

    def test_same_core_and_standby_transition_keep_active_connections(self):
        self.relay._observe_core_identity({'mihomo_pid': 100}, {'controller_port': 19090})
        self.relay._apply_connections({'connections': [{'id': 'same-pid'}]})
        self.manager.status.return_value = {
            'ok': True, 'running': False, 'core_running': True,
            'standby': True, 'runtime_mode': 'standby', 'mihomo_pid': 100,
        }
        self.relay._config_getter = lambda: {'routing': {'controller_port': 19090}}
        self.relay.set_view('connections')
        with mock.patch.object(self.relay, '_stream', side_effect=lambda *_: self.relay._stop.set()):
            self.relay._run()
        snapshot = self.relay.snapshot('connections')['snapshot']
        self.assertEqual([item['id'] for item in snapshot['active']], ['same-pid'])
        self.assertEqual(snapshot['closed'], [])

    def test_core_retirement_keeps_bounded_chronological_history(self):
        self.relay._apply_connections({'connections': [{'id': 'earlier'}]})
        self.relay._apply_connections({'connections': [
            {'id': str(index)} for index in range(MAX_CLOSED_CONNECTIONS + 5)
        ]})
        self.relay._retire_connections()
        snapshot = self.relay.snapshot('connections')['snapshot']
        self.assertEqual(snapshot['active'], [])
        self.assertEqual(len(snapshot['closed']), MAX_CLOSED_CONNECTIONS)
        self.assertEqual(snapshot['closed'][0]['id'], '5')
        self.assertEqual(snapshot['closed'][-1]['id'], str(MAX_CLOSED_CONNECTIONS + 4))

    def test_page_switch_during_frame_read_does_not_publish_old_snapshot(self):
        self.relay.set_view('connections')
        view, version = self.relay._active_snapshot()
        connection = mock.Mock()

        def read():
            self.relay.set_view('logs')
            return True, 1, b'{"connections": [{"id": "obsolete-view"}]}'

        with mock.patch('core.mihomo_activity._open_websocket', return_value=(connection, b'')), \
                mock.patch('core.mihomo_activity._FrameReader') as reader:
            reader.return_value.read.side_effect = read
            self.relay._stream(view, {}, version)
        self.assertEqual(self.relay.snapshot('connections')['snapshot']['active'], [])
        connection.close.assert_called_once()

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
