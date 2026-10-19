import json
import tempfile
import unittest
from unittest.mock import Mock, patch
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

    def test_rule_explanation_keeps_vpn_rule_and_reports_connection_state(self):
        cfg = self.config()
        name = cfg['rules'][0]['outbound'][4:]
        result = routing.explain_domain(cfg, 'mooc.chaoxing.com', [{'name': name, 'status': 'Disconnected'}])
        self.assertEqual(result['outbound'], 'vpn:' + name)
        self.assertTrue(result['available'])
        self.assertIn('物理接口直连', result['detail'])
        active = routing.explain_domain(cfg, 'mooc.chaoxing.com', [{'name': name, 'status': 'Connected'}])
        self.assertIn('VPN 已连接', active['detail'])

    def test_disconnected_vpn_resolves_rule_to_physical_by_default(self):
        config = self.config()
        name = config['rules'][0]['outbound'][4:]
        for vpns in ([{'name': name, 'status': 'Disconnected'}], [],
                      [{'name': name, 'status': 'Connecting'}]):
            with self.subTest(vpns=vpns):
                generated = routing.build_mihomo_config(config, vpns)
                self.assertIn('DOMAIN-SUFFIX,chaoxing.com,PHYSICAL', generated['rules'])
                self.assertNotIn('DOMAIN-SUFFIX,chaoxing.com,VPN-1', generated['rules'])
                self.assertFalse(any(p.get('cxvpn-physical-fallback') for p in generated['proxies']))
        generated = routing.build_mihomo_config(
            config, [{'name': name, 'status': 'Connected'}])
        self.assertIn('DOMAIN-SUFFIX,chaoxing.com,VPN-1', generated['rules'])
        self.assertNotIn('DOMAIN-SUFFIX,chaoxing.com,PHYSICAL', generated['rules'])
        # 组合字符与 emoji 出口经 JSON 序列化往返后仍生成同样的 VPN 规则。
        roundtrip = routing.normalize_config(json.loads(
            json.dumps(config, ensure_ascii=False)))
        generated = routing.build_mihomo_config(
            roundtrip, [{'name': name, 'status': 'Connected'}])
        self.assertIn('DOMAIN-SUFFIX,chaoxing.com,VPN-1', generated['rules'])

    def test_legacy_physical_fallback_field_is_stripped_by_normalize(self):
        for value in (True, False, 1, None, 'false'):
            with self.subTest(value=value):
                cfg = self.config()
                cfg['rules'][0]['physical_fallback'] = value
                normalized = routing.normalize_config(
                    json.loads(json.dumps(cfg, ensure_ascii=False)))
                self.assertNotIn('physical_fallback', normalized['rules'][0])
                self.assertEqual(normalized['rules'][0]['outbound'],
                                 self.config()['rules'][0]['outbound'])

    def test_vpn_connection_signature_limits_to_requested_names(self):
        rows = [{'name': 'A', 'status': 'Connected'},
                {'name': 'B', 'status': 'Disconnected'}]
        self.assertEqual(routing._vpn_connection_signature(rows),
                         {'A': True, 'B': False})
        self.assertEqual(routing._vpn_connection_signature(rows, ['B', 'C']),
                         {'B': False, 'C': False})
        self.assertEqual(routing._vpn_connection_signature(None, ['A']), {'A': False})

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


class StandbyBaselineTests(unittest.TestCase):
    def manager(self, capture_mode):
        manager = routing.RoutingManager.__new__(routing.RoutingManager)
        manager.log = Mock()
        manager._data_dir = tempfile.mkdtemp(prefix='cxvpn-standby-test-')
        manager._environment = Mock()
        manager._vpn_server_routes = Mock(return_value=([], []))
        manager._test_config = Mock()
        manager._install = Mock()
        manager._runtime_vpns = {'公司': True}
        manager._config = {'capture_mode': capture_mode}
        return manager

    def value(self, capture_mode):
        return routing.normalize_config({
            'capture_mode': capture_mode, 'physical_interface': '以太网',
            'rules': [{'enabled': True, 'match_type': 'suffix',
                       'domain': 'chaoxing.com', 'outbound': 'vpn:公司'}]})

    def test_system_proxy_standby_records_generation_vpn_state(self):
        manager = self.manager('system-proxy')
        manager._environment.snapshot.return_value = {
            'vpns': [{'name': '公司', 'status': 'Disconnected'}]}
        with patch.object(routing, 'verify_runtime'), \
                patch.object(routing, 'windows_system_proxy', return_value=''), \
                patch.object(routing, 'build_mihomo_config', return_value={'rules': []}) as build, \
                patch.object(routing, '_write_json'):
            manager._start_standby_runtime(self.value('system-proxy'))
        build_kwargs = build.call_args
        self.assertEqual(build_kwargs.args[1],
                         [{'name': '公司', 'status': 'Disconnected'}])
        manager._install.assert_called_once()
        self.assertEqual(manager._runtime_vpns, {'公司': False})

    def test_tun_standby_records_empty_vpn_view(self):
        manager = self.manager('tun')
        with patch.object(routing, 'verify_runtime'), \
                patch.object(routing, 'windows_system_proxy', return_value=''), \
                patch.object(routing, 'build_mihomo_config', return_value={'rules': []}) as build, \
                patch.object(routing, '_write_json'):
            manager._start_standby_runtime(self.value('tun'))
        self.assertEqual(build.call_args.args[1], [])
        self.assertTrue(build.call_args.kwargs.get('standby'))
        self.assertEqual(manager._runtime_vpns, {})
        manager._environment.snapshot.assert_not_called()

    def test_failed_standby_install_keeps_previous_baseline(self):
        manager = self.manager('system-proxy')
        manager._environment.snapshot.return_value = {
            'vpns': [{'name': '公司', 'status': 'Disconnected'}]}
        manager._install.side_effect = routing.RoutingError('端口被占用')
        with patch.object(routing, 'verify_runtime'), \
                patch.object(routing, 'windows_system_proxy', return_value=''), \
                patch.object(routing, 'build_mihomo_config', return_value={'rules': []}), \
                patch.object(routing, '_write_json'):
            with self.assertRaisesRegex(routing.RoutingError, '端口被占用'):
                manager._start_standby_runtime(self.value('system-proxy'))
        self.assertEqual(manager._runtime_vpns, {'公司': True})


class FastToggleVpnGuardTests(unittest.TestCase):
    def setUp(self):
        self.value = routing.normalize_config({
            'enabled': True, 'capture_mode': 'system-proxy',
            'physical_interface': '以太网',
            'rules': [{'enabled': True, 'match_type': 'suffix',
                       'domain': 'chaoxing.com', 'outbound': 'vpn:公司'}]})
        self.manager = routing.RoutingManager()
        self.manager._runtime_signature = self.manager._config_signature(self.value)
        self.manager._runtime_sha256 = 'ABC'
        self.manager._runtime_vpns = {'公司': True}
        self.manager._service_state = Mock(return_value={
            'installed': True, 'backend': 'native', 'state': 'Running',
            'runtime_running': True, 'runtime_mode': 'standby',
            'fast_toggle_ready': True, 'config_sha256': 'ABC',
            'service_compatible': True, 'pending_transaction': False,
            'crash_fused': False})
        self.manager._environment = Mock()
        self.manager._fast_toggle_system_proxy = Mock(return_value={
            'ok': True, 'msg': '代理已开启', 'config': self.value,
            'warnings': [], 'status': {}, 'standby_pending': False})

    def test_vpn_state_drift_downgrades_fast_toggle_to_full_apply(self):
        self.manager._environment.snapshot.return_value = {
            'vpns': [{'name': '公司', 'status': 'Disconnected'}]}
        with patch.object(routing, 'verify_runtime',
                          side_effect=routing.RoutingError('full apply reached')):
            with self.assertRaisesRegex(routing.RoutingError, 'full apply reached'):
                self.manager.apply(self.value, allow_fast_toggle=True)
        self.manager._fast_toggle_system_proxy.assert_not_called()

    def test_matching_vpn_state_keeps_fast_toggle(self):
        self.manager._environment.snapshot.return_value = {
            'vpns': [{'name': '公司', 'status': 'Connected'}]}
        result = self.manager.apply(self.value, allow_fast_toggle=True)
        self.assertTrue(result['ok'])
        self.manager._fast_toggle_system_proxy.assert_called_once_with(self.value, True)
        self.manager._environment.snapshot.assert_called_once_with(False, fresh=True)

    def test_unknown_baseline_keeps_fast_toggle_without_scan(self):
        self.manager._runtime_vpns = None
        result = self.manager.apply(self.value, allow_fast_toggle=True)
        self.assertTrue(result['ok'])
        self.manager._fast_toggle_system_proxy.assert_called_once()
        self.manager._environment.snapshot.assert_not_called()

    def test_config_without_vpn_outbounds_skips_the_guard(self):
        self.value['rules'] = [{'enabled': True, 'match_type': 'suffix',
                                'domain': 'chaoxing.com', 'outbound': 'physical'}]
        self.manager._runtime_signature = self.manager._config_signature(self.value)
        result = self.manager.apply(self.value, allow_fast_toggle=True)
        self.assertTrue(result['ok'])
        self.manager._fast_toggle_system_proxy.assert_called_once()
        self.manager._environment.snapshot.assert_not_called()
