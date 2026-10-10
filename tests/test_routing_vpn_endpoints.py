# -*- coding: utf-8 -*-
import copy
import json
import socket
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from core import routing


def profile(server='vpn.example.test', name='孙河 VPN e\u0301 🇨🇳'):
    return {'name': name, 'server': server, 'status': 'Disconnected'}


def dns_row(address, family=socket.AF_INET):
    return (family, socket.SOCK_STREAM, 6, '', (address, 0))


class VpnEndpointTests(unittest.TestCase):
    def test_disconnected_unreferenced_vpn_uses_real_dns_and_physical_rule(self):
        config = routing.normalize_config({
            'traffic_mode': 'global', 'default_outbound': 'physical',
            'capture_mode': 'tun', 'physical_interface': '以太网',
            'rules': [{'domain': 'example.test', 'outbound': 'block'}],
        })
        previous = copy.deepcopy(config)
        generated = routing.build_mihomo_config(config, [profile()])
        self.assertIn('vpn.example.test', generated['dns']['fake-ip-filter'])
        self.assertEqual(generated['dns']['nameserver-policy']['vpn.example.test'],
                         routing.default_config()['direct_nameserver'])
        self.assertEqual(generated['rules'][0], 'DOMAIN,vpn.example.test,PHYSICAL')
        self.assertEqual(config, previous)

    def test_unicode_vpn_binding_survives_utf8_runtime_json(self):
        vpn = profile()
        vpn['status'] = 'Connected'
        config = routing.normalize_config({'rules': [
            {'match_type': 'exact', 'domain': 'business.test',
             'outbound': 'vpn:' + vpn['name']}],
            'capture_mode': 'tun', 'physical_interface': '以太网'})
        generated = routing.build_mihomo_config(config, [vpn])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            path.write_text(json.dumps(generated, ensure_ascii=False), encoding='utf-8')
            restored = json.loads(path.read_text(encoding='utf-8'))
        binding = next(row for row in restored['proxies'] if row['name'] == 'VPN-1')
        self.assertEqual(binding['interface-name'], '孙河 VPN e\u0301 🇨🇳')
        self.assertIn('DOMAIN,business.test,VPN-1', restored['rules'])

    def test_advanced_dns_keeps_draft_and_unrelated_policy(self):
        config = routing.normalize_config({
            'dns_mode': 'advanced', 'direct_nameserver': ['119.29.29.29'],
            'fake_ip_filter': ['+.lan', 'vpn.example.test'],
            'nameserver_policy': [{'domain': 'corp.test', 'servers': ['1.1.1.1']}],
        })
        dns = routing.build_mihomo_config(config, [profile(), profile()])['dns']
        self.assertEqual(dns['fake-ip-filter'], ['+.lan', 'vpn.example.test'])
        self.assertEqual(dns['nameserver-policy']['+.corp.test'], ['1.1.1.1'])
        self.assertEqual(dns['nameserver-policy']['vpn.example.test'], ['119.29.29.29'])
        self.assertEqual(config['nameserver_policy'],
                         [{'domain': 'corp.test', 'servers': ['1.1.1.1']}])

    def test_redir_host_standby_and_ip_servers_are_supported(self):
        config = routing.normalize_config({'dns_mode': 'advanced',
                                           'dns_enhanced_mode': 'redir-host'})
        generated = routing.build_mihomo_config(config, [profile('VPN.EXAMPLE.TEST.'),
            profile('203.0.113.1'), profile('2001:db8::1')], standby=True)
        self.assertNotIn('fake-ip-filter', generated['dns'])
        self.assertEqual(list(generated['dns']['nameserver-policy']), ['vpn.example.test'])
        self.assertEqual(generated['rules'][0], 'DOMAIN,vpn.example.test,PHYSICAL')
        self.assertFalse(generated['tun']['enable'])

    def test_real_system_results_keep_both_address_families(self):
        lookup = mock.Mock()
        with mock.patch.object(routing.socket, 'getaddrinfo', return_value=[
                dns_row('203.0.113.1'), dns_row('2001:db8::1', socket.AF_INET6)]):
            routes, warnings = routing._resolve_vpn_server_routes(
                [profile()], {profile()['name']}, lookup)
        self.assertEqual(routes, ['2001:db8::1/128', '203.0.113.1/32'])
        self.assertEqual(warnings, [])
        lookup.assert_not_called()

    def test_fake_ip_is_replaced_with_real_controller_results(self):
        for address in ('198.18.1.49', '198.19.1.49', 'fdfe:dcba:9876::123'):
            with self.subTest(address=address), mock.patch.object(
                    routing.socket, 'getaddrinfo', return_value=[dns_row(address)]):
                routes, warnings = routing._resolve_vpn_server_routes(
                    [profile()], {profile()['name']}, lambda _: ['203.0.113.1'])
            self.assertEqual(routes, ['203.0.113.1/32'])
            self.assertEqual(warnings, [])

    def test_failed_system_dns_falls_back_to_controller(self):
        with mock.patch.object(routing.socket, 'getaddrinfo', side_effect=socket.gaierror()):
            routes, warnings = routing._resolve_vpn_server_routes(
                [profile()], {profile()['name']}, lambda _: ['203.0.113.1'])
        self.assertEqual(routes, ['203.0.113.1/32'])
        self.assertEqual(warnings, [])

    def test_fake_empty_and_failed_controller_results_never_become_exclusions(self):
        for result in ([], ['198.18.1.49'], ['not-an-address']):
            with self.subTest(result=result), mock.patch.object(
                    routing.socket, 'getaddrinfo', return_value=[dns_row('198.18.1.49')]):
                routes, warnings = routing._resolve_vpn_server_routes(
                    [profile()], {profile()['name']}, lambda _: result)
            self.assertEqual(routes, [])
            self.assertEqual(len(warnings), 1)
        with mock.patch.object(routing.socket, 'getaddrinfo', return_value=[dns_row('198.18.1.49')]):
            routes, warnings = routing._resolve_vpn_server_routes([profile()], {profile()['name']})
        self.assertEqual(routes, [])
        self.assertEqual(len(warnings), 1)

    def test_literal_fake_server_is_rejected_and_real_literal_is_kept(self):
        routes, warnings = routing._resolve_vpn_server_routes([
            profile('198.18.1.49'), profile('203.0.113.2')], {profile()['name']})
        self.assertEqual(routes, ['203.0.113.2/32'])
        self.assertEqual(len(warnings), 1)

    def test_manager_keeps_a_result_when_aaaa_times_out(self):
        logs = []
        manager = routing.RoutingManager(logger=logs.append)
        config = routing.normalize_config({})
        with mock.patch.object(routing.socket, 'getaddrinfo', return_value=[dns_row('198.18.1.49')]), \
                mock.patch.object(manager, '_controller_request', side_effect=[
                    {'Answer': [{'type': 5, 'data': 'alias.test.'},
                                {'type': 1, 'data': '203.0.113.3'}]}, TimeoutError()]):
            routes, warnings = manager._vpn_server_routes(config, [profile()])
        self.assertEqual(routes, ['203.0.113.3/32'])
        self.assertEqual(warnings, [])
        self.assertTrue(any('AAAA' in line and 'TimeoutError' in line for line in logs))


if __name__ == '__main__':
    unittest.main()
