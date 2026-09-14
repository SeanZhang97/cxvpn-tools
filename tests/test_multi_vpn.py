# -*- coding: utf-8 -*-
import os
import tempfile
import threading
import unittest
import ctypes
from unittest.mock import Mock, patch

from api import Api
from core import vpn_connect, vpn_os
from core.worker import Worker


def profile(name, status='Disconnected', routes=None):
    routes = routes or []
    return {
        'name': name,
        'server': f'{name}.example.test',
        'type': 'Pptp',
        'status': status,
        'ip_address': '',
        'prefix_length': 0,
        'routes': routes,
        'default_route_ipv4': '0.0.0.0/0' in routes,
        'default_route_ipv6': '::/0' in routes,
        'default_route': ('0.0.0.0/0' in routes or '::/0' in routes),
        'ipv4_default_gateway': True,
        'ipv6_default_gateway': True,
    }


class RouteAnalysisTests(unittest.TestCase):
    def test_detects_multiple_defaults_and_overlapping_subnets(self):
        rows = [
            profile('VPN A', 'Connected', ['0.0.0.0/0', '10.0.0.0/8']),
            profile('VPN B', 'Connected', ['0.0.0.0/0', '10.1.0.0/16']),
        ]

        conflicts = vpn_os.analyze_route_conflicts(rows)

        self.assertEqual(
            {row['type'] for row in conflicts},
            {'default_route_ipv4', 'subnet_overlap'})

    def test_detects_ipv6_default_and_subnet_conflicts(self):
        rows = [
            profile('VPN A', 'Connected', ['::/0', '2001:db8::/32']),
            profile('VPN B', 'Connected', ['::/0', '2001:db8:1::/48']),
        ]

        conflicts = vpn_os.analyze_route_conflicts(rows)

        self.assertEqual(
            {row['type'] for row in conflicts},
            {'default_route_ipv6', 'subnet_overlap'})


class GatewaySettingsTests(unittest.TestCase):
    def test_reads_and_updates_ipv4_ipv6_independently(self):
        with tempfile.TemporaryDirectory() as folder:
            phonebook = os.path.join(folder, 'rasphone.pbk')
            source = ('[工作 VPN]\r\nIpPrioritizeRemote=1\r\n'
                      'Ipv6PrioritizeRemote=1\r\nOtherValue=keep\r\n\r\n'
                      '[备用 VPN]\r\nIpPrioritizeRemote=1\r\n')
            with open(phonebook, 'wb') as stream:
                stream.write(source.encode('utf-8'))

            ok, message = vpn_os.set_gateway_settings(
                '工作 VPN', False, True, phonebook=phonebook)
            settings = vpn_os._read_gateway_settings(phonebook)

            self.assertTrue(ok, message)
            self.assertEqual(settings['工作 vpn'], {
                'ipv4_default_gateway': False,
                'ipv6_default_gateway': True,
            })
            with open(phonebook, 'rb') as stream:
                updated = stream.read().decode('utf-8')
            self.assertIn('OtherValue=keep', updated)
            self.assertIn('[备用 VPN]', updated)

    def test_adds_missing_gateway_fields_to_existing_entry(self):
        with tempfile.TemporaryDirectory() as folder:
            phonebook = os.path.join(folder, 'rasphone.pbk')
            with open(phonebook, 'wb') as stream:
                stream.write('[工作 VPN]\nOtherValue=keep\n'.encode('utf-8'))

            ok, message = vpn_os.set_gateway_settings(
                '工作 VPN', True, False, phonebook=phonebook)

            self.assertTrue(ok, message)
            with open(phonebook, 'rb') as stream:
                updated = stream.read().decode('utf-8')
            self.assertIn('IpPrioritizeRemote=1', updated)
            self.assertIn('Ipv6PrioritizeRemote=0', updated)


class ConnectionStatusTests(unittest.TestCase):
    @patch('core.vpn_connect.vpn_os._ps', return_value=(
        True, 'Disconnected', ''))
    @patch('core.vpn_connect.status', return_value=['工作 VPN 备用'])
    def test_connection_name_match_is_exact(self, status, ps):
        vpn_connect._CONN_CACHE.clear()

        connected = vpn_connect.is_connected('工作 VPN', refresh=True)

        self.assertFalse(connected)
        ps.assert_called_once()


class RasConnectionDurationTests(unittest.TestCase):
    def test_reads_duration_for_each_active_ras_handle(self):
        class FakeRasApi:
            def __init__(self):
                self.enum_calls = 0

                def enum_connections(rows, size_ptr, count_ptr):
                    self.enum_calls += 1
                    size = ctypes.cast(
                        size_ptr, ctypes.POINTER(ctypes.c_uint32))
                    count = ctypes.cast(
                        count_ptr, ctypes.POINTER(ctypes.c_uint32))
                    if self.enum_calls == 1:
                        size[0] = ctypes.sizeof(vpn_connect._RASCONN) * 2
                        count[0] = 2
                        return vpn_connect.ERROR_BUFFER_TOO_SMALL
                    rows[0].hrasconn = 101
                    rows[0].szEntryName = 'VPN A'
                    rows[1].hrasconn = 202
                    rows[1].szEntryName = 'VPN B'
                    count[0] = 2
                    return vpn_connect.ERROR_SUCCESS

                def get_statistics(handle, stats_ptr):
                    stats = ctypes.cast(
                        stats_ptr,
                        ctypes.POINTER(vpn_connect._RAS_STATS)).contents
                    stats.dwConnectDuration = {
                        101: 3_600_000,
                        202: 125_000,
                    }[int(handle)]
                    return vpn_connect.ERROR_SUCCESS

                self.RasEnumConnectionsW = enum_connections
                self.RasGetConnectionStatistics = get_statistics

        result = vpn_connect.connection_durations(FakeRasApi())

        self.assertEqual(result, {'VPN A': 3600.0, 'VPN B': 125.0})


class WorkerMultiConnectionTests(unittest.TestCase):
    @patch('core.vpn_connect.status', return_value=['工作 vpn'])
    @patch('core.vpn_os.list_vpns')
    def test_system_ras_state_overrides_stale_profile_status(
            self, system_list, ras_status):
        system_list.return_value = [profile('工作 VPN', 'Disconnected')]
        worker = Worker(lambda: {'vpn_name': '工作 VPN'}, Mock())

        rows = worker.refresh_connections()
        snapshot = worker.connection_snapshot('工作 VPN')

        self.assertEqual(rows[0]['status'], 'Connected')
        self.assertEqual(snapshot['connected_names'], ['工作 VPN'])
        self.assertTrue(snapshot['default_connected'])

    @patch('core.worker.time.time', side_effect=[100.0, 160.0])
    def test_snapshot_tracks_each_connection_and_default(self, now):
        worker = Worker(lambda: {'vpn_name': 'VPN B'}, Mock())
        rows = [profile('VPN A', 'Connected'), profile('VPN B', 'Connected')]

        worker.refresh_connections(rows)
        snapshot = worker.connection_snapshot('VPN B')

        self.assertTrue(snapshot['default_connected'])
        self.assertEqual(snapshot['connected_names'], ['VPN A', 'VPN B'])
        self.assertEqual(snapshot['connected_count'], 2)
        self.assertEqual(snapshot['connections'][0]['connected_seconds'], 60)

    @patch('core.worker.time.time', side_effect=[10000.0, 10060.0])
    @patch('core.vpn_connect.connection_durations', return_value={
        'vpn a': 3600.0})
    @patch('core.vpn_connect.status', return_value=['VPN A'])
    @patch('core.vpn_os.list_vpns')
    def test_snapshot_uses_windows_ras_connection_duration(
            self, system_list, ras_status, durations, now):
        system_list.return_value = [profile('VPN A', 'Connected')]
        worker = Worker(lambda: {'vpn_name': 'VPN A'}, Mock())

        worker.refresh_connections()
        snapshot = worker.connection_snapshot('VPN A')

        connection = snapshot['connections'][0]
        self.assertEqual(connection['connected_seconds'], 3660)
        self.assertEqual(connection['connected_at'], 6400.0)
        self.assertEqual(connection['connection_time_source'], 'system')

    def test_complete_profile_snapshot_is_reused_without_system_query(self):
        worker = Worker(lambda: {'vpn_name': 'VPN A'}, Mock())
        rows = [profile('VPN A'), profile('VPN B', 'Connected')]
        worker.refresh_connections(rows)

        with patch('core.vpn_os.list_vpns') as system_list:
            cached = worker.list_profiles()

        self.assertEqual([row['name'] for row in cached], ['VPN A', 'VPN B'])
        system_list.assert_not_called()


class MultiVpnApiTests(unittest.TestCase):
    def make_api(self, rows):
        instance = Api.__new__(Api)
        instance._lock = threading.Lock()
        instance._vpn_action_lock = threading.Lock()
        instance.cfg = {'vpn_name': 'VPN A', 'creds': {}}
        instance.worker = Mock()
        instance.worker.state = {}
        instance.worker.refresh_connections.return_value = rows
        instance.worker.list_profiles.return_value = rows
        instance.worker.connection_snapshot.return_value = {
            'connections': [], 'connected_names': [],
            'connected_count': 0, 'route_conflicts': []}
        instance.log = Mock()
        return instance

    def test_list_vpns_uses_background_profile_cache_by_default(self):
        rows = [profile('VPN A'), profile('VPN B')]
        instance = self.make_api(rows)

        result = instance.list_vpns()

        self.assertEqual(result, rows)
        instance.worker.list_profiles.assert_called_once_with(False)

    def test_manual_list_refresh_forces_windows_query(self):
        rows = [profile('VPN A')]
        instance = self.make_api(rows)

        instance.list_vpns(True)

        instance.worker.list_profiles.assert_called_once_with(True)

    @patch('api.vpn_os.set_vpn', return_value=(True, '已更新'))
    def test_update_passes_independent_gateway_settings(self, set_vpn):
        instance = self.make_api([profile('VPN A')])

        result = instance.update_vpn(
            'VPN A', 'vpn.example.test', 'Pptp', '', False, True)

        self.assertTrue(result['ok'])
        set_vpn.assert_called_once_with(
            'VPN A', 'vpn.example.test', 'Pptp', l2tp_psk='',
            ipv4_default_gateway=False, ipv6_default_gateway=True)

    @patch('api.vpn_service.connect')
    def test_second_connection_requires_explicit_mode(self, connect):
        rows = [profile('VPN A', 'Connected'), profile('VPN B')]
        instance = self.make_api(rows)

        result = instance.connect_vpn('VPN B')

        self.assertFalse(result['ok'])
        self.assertTrue(result['needs_mode_choice'])
        self.assertEqual(result['active_names'], ['VPN A'])
        connect.assert_not_called()

    @patch('api.vpn_service.connect')
    def test_pending_connection_request_is_not_submitted_again(self, connect):
        rows = [profile('VPN A')]
        instance = self.make_api(rows)
        instance.worker.connection_pending.return_value = True

        result = instance.connect_vpn('VPN A')

        self.assertTrue(result['ok'])
        self.assertTrue(result['pending'])
        connect.assert_not_called()

    @patch('api.vpn_service.connect')
    def test_manual_connect_returns_immediately_while_background_dial_runs(
            self, connect):
        instance = self.make_api([profile('VPN A'), profile('VPN B')])
        instance.worker.dial_in_progress.return_value = {
            'name': 'VPN A', 'source': 'automatic'}

        result = instance.connect_vpn('VPN B')

        self.assertFalse(result['ok'])
        self.assertTrue(result['busy'])
        self.assertIn('VPN A', result['msg'])
        instance.worker.manual_connection_action.assert_not_called()
        connect.assert_not_called()

    @patch('api.vpn_connect.disconnect', return_value=(True, '已断开'))
    @patch('api.vpn_service.connect', return_value=(True, '已连接'))
    def test_switch_disconnects_existing_before_connecting_selected(
            self, connect, disconnect):
        before = [profile('VPN A', 'Connected'), profile('VPN B')]
        after = [profile('VPN A'), profile('VPN B', 'Connected')]
        instance = self.make_api(before)
        instance.worker.refresh_connections.side_effect = [before, after]

        result = instance.connect_vpn('VPN B', 'switch')

        self.assertTrue(result['ok'])
        disconnect.assert_called_once_with('VPN A')
        connect.assert_called_once_with('VPN B', None, log=instance.log)
        instance.worker.prepare_connection_target.assert_called_once_with(
            'VPN B', ['VPN A'])
        instance.worker._set_connection_action.assert_called_with(
            'connected', 'manual', 'VPN B', '已连接 VPN B')
        instance.worker.manual_connection_action.assert_called_once_with()

    @patch('api.vpn_connect.disconnect')
    @patch('api.vpn_service.connect', return_value=(True, '已连接'))
    def test_parallel_connection_keeps_existing_connection(
            self, connect, disconnect):
        before = [profile('VPN A', 'Connected'), profile('VPN B')]
        after = [profile('VPN A', 'Connected'), profile('VPN B', 'Connected')]
        instance = self.make_api(before)
        instance.worker.refresh_connections.side_effect = [before, after]

        result = instance.connect_vpn('VPN B', 'parallel')

        self.assertTrue(result['ok'])
        disconnect.assert_not_called()
        connect.assert_called_once_with('VPN B', None, log=instance.log)
        instance.worker._set_connection_action.assert_called_with(
            'connected', 'manual', 'VPN B', '已连接 VPN B')

    @patch('api.vpn_connect.disconnect', return_value=(False, 'RAS 错误 828'))
    @patch('api.vpn_service.connect')
    def test_switch_disconnect_failure_finishes_visible_action(
            self, connect, disconnect):
        rows = [profile('VPN A', 'Connected'), profile('VPN B')]
        instance = self.make_api(rows)

        result = instance.connect_vpn('VPN B', 'switch')

        self.assertFalse(result['ok'])
        instance.worker._set_connection_action.assert_called_with(
            'failed', 'manual', 'VPN B', result['msg'])
        connect.assert_not_called()

    @patch('api.vpn_service.connect')
    def test_already_connected_target_replaces_previous_action(self, connect):
        rows = [profile('VPN A'), profile('VPN B', 'Connected')]
        instance = self.make_api(rows)

        result = instance.connect_vpn('VPN B')

        self.assertTrue(result['ok'])
        instance.worker.prepare_connection_target.assert_called_once_with('VPN B')
        instance.worker._set_connection_action.assert_called_with(
            'connected', 'manual', 'VPN B', '已连接 VPN B')
        connect.assert_not_called()

    @patch('api.vpn_connect.disconnect', return_value=(True, '已断开'))
    def test_disconnect_all_operates_on_every_connected_name(self, disconnect):
        before = [profile('VPN A', 'Connected'), profile('VPN B', 'Connected')]
        after = [profile('VPN A'), profile('VPN B')]
        instance = self.make_api(before)
        instance.worker.refresh_connections.side_effect = [before, after]

        result = instance.disconnect_all()

        self.assertTrue(result['ok'])
        self.assertEqual(
            [call.args[0] for call in disconnect.call_args_list],
            ['VPN A', 'VPN B'])
        instance.worker.note_manual_disconnect.assert_called_once_with(
            ['VPN A', 'VPN B'])

    @patch('api.cfgmod.save')
    def test_frontend_config_save_preserves_worker_authorization(self, save):
        instance = self.make_api([profile('VPN A')])
        instance.cfg['authorization'] = {
            'last_success_at': 1000, 'expires_at': 2000}
        instance.cfg['phone'] = '13800000000'
        instance.cfg['creds'] = {
            'VPN A': {'user': 'alice', 'pass': 'secret'}}

        instance.save_config({'vpn_name': 'VPN B', 'auto_connect': True})

        self.assertEqual(instance.cfg['authorization']['expires_at'], 2000)
        self.assertEqual(instance.cfg['phone'], '13800000000')
        self.assertEqual(instance.cfg['creds']['VPN A']['user'], 'alice')
        instance.worker.update_default_target.assert_called_once_with('VPN B')
        instance.worker.reset_backoff.assert_not_called()

    @patch('api.cfgmod.save')
    def test_frontend_rejects_stale_full_config_before_overwriting_credentials(
            self, save):
        instance = self.make_api([profile('VPN A')])
        instance.cfg['creds'] = {
            'VPN A': {'user': 'alice', 'pass': 'secret'}}

        with self.assertRaisesRegex(ValueError, '过期的完整配置'):
            instance.save_config({'auto_connect': True, 'creds': {}})

        save.assert_not_called()
        self.assertEqual(instance.cfg['creds']['VPN A']['user'], 'alice')

    @patch('api.cfgmod.save')
    def test_enabling_auto_connect_uses_non_destructive_resume(self, save):
        instance = self.make_api([profile('VPN A')])
        instance.cfg['auto_connect'] = False

        instance.save_config({'vpn_name': 'VPN A', 'auto_connect': True})

        instance.worker.enable_auto_connect.assert_called_once_with()
        instance.worker.update_default_target.assert_not_called()
        instance.worker.reset_backoff.assert_not_called()

    @patch('api.cfgmod.save')
    @patch('api.vpn_service.sync_credentials', return_value=(True, 0))
    @patch('api.vpn_service.connect')
    def test_credential_save_does_not_require_connection_mode(
            self, connect, sync, save):
        rows = [profile('VPN A', 'Connected'), profile('VPN B')]
        instance = self.make_api(rows)

        result = instance.save_cred('VPN B', 'alice', 'secret')

        self.assertTrue(result['ok'])
        sync.assert_called_once_with('VPN B', 'alice', 'secret')
        connect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
