import unittest
from unittest.mock import patch
from core import routing


class VpnRouteCapabilityTests(unittest.TestCase):
    def config(self):
        return routing.normalize_config({
            'physical_interface': '以太网', 'default_outbound': 'physical',
            'rules': [{'enabled': True, 'match_type': 'suffix', 'domain': 'chaoxing.com',
                       'outbound': 'vpn:公司 e\u0301 \U0001f1e8\U0001f1f3'}]})

    def test_only_active_vpn_outbound_gets_managed_route_capability(self):
        config = self.config()
        generated = routing.build_mihomo_config(config, [])
        self.assertTrue(generated['proxies'][1]['cxvpn-managed-route'])
        self.assertNotIn('cxvpn-managed-route', generated['proxies'][0])
        self.assertEqual(generated['proxies'][1]['interface-name'], '公司 e\u0301 \U0001f1e8\U0001f1f3')
        standby = routing.build_mihomo_config(config, [], standby=True)
        self.assertFalse(any(p.get('cxvpn-managed-route') for p in standby['proxies']))

    def test_old_core_cannot_silently_accept_managed_vpn(self):
        manager = routing.RoutingManager.__new__(routing.RoutingManager)
        manager.log = lambda _: None
        with patch.object(manager, '_controller_version', return_value='v1.19.30'), \
                patch.object(manager, '_controller_request', return_value={'version': 'v1.19.30'}):
            with self.assertRaisesRegex(routing.RoutingError, '不支持 VPN'):
                manager._wait_native_ready(self.config())

    def test_previous_managed_core_without_fallback_cannot_be_reused(self):
        manager = routing.RoutingManager.__new__(routing.RoutingManager)
        manager.log = lambda _: None
        with patch.object(manager, '_controller_version', return_value='v1.19.30-cxvpn.1'), \
                patch.object(manager, '_controller_request', return_value={'cxvpn-vpn-routes': 1}):
            with self.assertRaisesRegex(routing.RoutingError, '不支持 VPN'):
                manager._wait_native_ready(self.config())

    def test_rule_explanation_keeps_vpn_rule_and_reports_physical_fallback(self):
        cfg = self.config()
        name = cfg['rules'][0]['outbound'][4:]
        result = routing.explain_domain(cfg, 'mooc.chaoxing.com', [{'name': name, 'status': 'Disconnected'}])
        self.assertEqual(result['outbound'], 'vpn:' + name)
        self.assertTrue(result['available'])
        self.assertIn('物理接口直连', result['detail'])
        active = routing.explain_domain(cfg, 'mooc.chaoxing.com', [{'name': name, 'status': 'Connected'}])
        self.assertIn('VPN 已连接', active['detail'])
