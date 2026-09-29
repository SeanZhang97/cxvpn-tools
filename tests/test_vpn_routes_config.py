import json
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
        with patch.object(manager, '_check_native_runtime'), \
                patch.object(manager, '_controller_version', return_value='v1.19.30'), \
                patch.object(manager, '_controller_request', return_value={'version': 'v1.19.30'}):
            with self.assertRaisesRegex(routing.RoutingError, '不支持 VPN'):
                manager._wait_native_ready(self.config())

    def test_previous_managed_core_without_fallback_cannot_be_reused(self):
        manager = routing.RoutingManager.__new__(routing.RoutingManager)
        manager.log = lambda _: None
        with patch.object(manager, '_check_native_runtime'), \
                patch.object(manager, '_controller_version', return_value='v1.19.30-cxvpn.2'), \
                patch.object(manager, '_controller_request', return_value={'cxvpn-vpn-routes': 2}):
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

    def test_fallback_is_opt_in_per_rule_and_survives_utf8_roundtrip(self):
        config = self.config()
        self.assertFalse(config['rules'][0]['physical_fallback'])
        config['rules'].append({
            'match_type': 'exact', 'domain': 'public.example.com',
            'outbound': config['rules'][0]['outbound'], 'physical_fallback': True,
        })
        config = routing.normalize_config(json.loads(json.dumps(config, ensure_ascii=False)))
        generated = routing.build_mihomo_config(config, [])
        proxies = {p['name']: p for p in generated['proxies']}
        self.assertNotIn('cxvpn-physical-fallback', proxies['VPN-1'])
        self.assertNotIn('cxvpn-physical-fallback', proxies['PHYSICAL'])
        self.assertTrue(proxies['VPN-1-FALLBACK']['cxvpn-managed-route'])
        self.assertTrue(proxies['VPN-1-FALLBACK']['cxvpn-physical-fallback'])
        self.assertEqual(proxies['VPN-1-FALLBACK']['interface-name'],
                         config['rules'][0]['outbound'][4:])
        self.assertIn('DOMAIN-SUFFIX,chaoxing.com,VPN-1', generated['rules'])
        self.assertIn('DOMAIN,public.example.com,VPN-1-FALLBACK', generated['rules'])
        for options in ({'standby': True}, {}):
            if not options:
                config['traffic_mode'] = 'global'
            inactive = routing.build_mihomo_config(config, [], **options)
            self.assertFalse(any(p.get('cxvpn-physical-fallback') for p in inactive['proxies']))

    def test_disabled_and_non_vpn_rules_cannot_authorize_fallback(self):
        cfg = self.config()
        cfg['rules'][0].update(physical_fallback=True, enabled=False)
        self.assertFalse(any(p.get('cxvpn-physical-fallback') for p in
                             routing.build_mihomo_config(cfg, [])['proxies']))
        cfg['rules'][0].update(outbound='physical', enabled=True)
        self.assertFalse(routing.normalize_config(cfg)['rules'][0]['physical_fallback'])
        for value in ('false', 1, None):
            cfg['rules'][0]['physical_fallback'] = value
            with self.subTest(value=value), self.assertRaises(routing.RoutingError):
                routing.normalize_config(cfg)

    def test_authorization_entry_uses_physical_before_broad_vpn_rule(self):
        for mode in ('rule', 'global'):
            cfg = self.config()
            cfg['traffic_mode'] = mode
            generated = routing.build_mihomo_config(cfg, [])
            self.assertEqual(generated['rules'][0], 'DOMAIN,remote.chaoxing.com,PHYSICAL')
            self.assertEqual(routing.explain_domain(cfg, 'remote.chaoxing.com')['outbound'],
                             'physical')
        cfg = self.config()
        self.assertEqual(routing.explain_domain(cfg, 'other.chaoxing.com')['outbound'],
                         cfg['rules'][0]['outbound'])

    def test_authorization_exception_does_not_override_explicit_block(self):
        for kind, domain in (('exact', 'remote.chaoxing.com'), ('suffix', 'chaoxing.com'),
                             ('wildcard', '*.chaoxing.com')):
            cfg = self.config()
            cfg['rules'] = [{'match_type': kind, 'domain': domain, 'outbound': 'block'}]
            cfg = routing.normalize_config(cfg)
            generated = routing.build_mihomo_config(cfg, [])
            self.assertNotIn('DOMAIN,remote.chaoxing.com,PHYSICAL', generated['rules'])
            self.assertEqual(routing.explain_domain(cfg, 'remote.chaoxing.com')['outbound'],
                             'block')
