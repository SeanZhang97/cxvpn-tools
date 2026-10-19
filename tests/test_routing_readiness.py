# -*- coding: utf-8 -*-
"""代理出口就绪、独立探测和更新失效提示；不连接真实服务。"""
import copy
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from core import routing, routing_business as business, routing_service, subscription_store
from tests.test_routing_business import sample


class RoutingReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cxvpn-readiness-')
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {'LOCALAPPDATA': self.temp.name})
        env.start(); self.addCleanup(env.stop)
        self.value = sample()
        self.value['capture_mode'] = 'system-proxy'
        self.manager = routing.RoutingManager.__new__(routing.RoutingManager)
        self.manager.log = Mock()
        self.manager._native_service = Mock()
        self.manager._native_service.status.return_value = {'runtime_running': True}
        self.manager._controller_version = Mock(return_value='offline')
        self.manager._probe_connectivity = Mock()

    def states(self, rejected=False):
        return {'OUTBOUND-' + item['id']: {
            'all': ['REJECT' if rejected else '[订阅甲] ' + item['node_name']],
            'now': 'REJECT' if rejected else '[订阅甲] ' + item['node_name']}
            for item in self.value['proxy_outbounds']}

    def clock(self):
        value = [0.0]
        monotonic = patch.object(routing.time, 'monotonic', side_effect=lambda: value[0])
        sleep = patch.object(routing.time, 'sleep', side_effect=lambda delay: value.__setitem__(0, value[0] + delay))
        monotonic.start(); sleep.start()
        self.addCleanup(monotonic.stop); self.addCleanup(sleep.stop)
        return value

    def test_business_only_missing_nodes_warn_instead_of_clean_success(self):
        clock = self.clock()
        states = self.states(rejected=True)
        self.manager._controller_request = Mock(side_effect=lambda cfg, path, **kw:
            {'proxies': states} if path == '/proxies' else states[path.split('/')[-1]])
        warnings = self.manager._wait_native_ready(self.value)
        self.assertEqual(len(warnings), 2)
        self.assertTrue(all('拒绝连接' in item for item in warnings))
        self.assertLessEqual(clock[0], 25)
        self.assertGreater(clock[0], 20)
        self.assertFalse(any('/delay?' in call.args[1] for call in self.manager._controller_request.call_args_list))

    def test_delayed_business_nodes_are_retried_without_legacy_manual_selection(self):
        self.clock()
        self.manager._controller_request = Mock(side_effect=[{'proxies': self.states(True)}, {'proxies': self.states()}])
        self.manager._verify_runtime_egress = Mock(return_value=[])
        self.assertEqual(self.manager._wait_native_ready(self.value), [])
        self.assertEqual([call.args[1] for call in self.manager._controller_request.call_args_list], ['/proxies', '/proxies'])

    def test_readiness_deadline_is_shared_across_many_timeouts(self):
        clock = self.clock()
        value = routing.normalize_config({'default_outbound': 'proxy', 'proxy_providers': [
            {'id': f'p{i}', 'name': f'P{i}', 'url': 'https://example.test/sub', 'enabled': True}
            for i in range(16)]})
        def request(cfg, path, timeout=2.5, **kw):
            clock[0] += timeout
            raise OSError('offline timeout')
        self.manager._controller_request = Mock(side_effect=request)
        with self.assertRaisesRegex(routing.RoutingError, '总期限'):
            self.manager._wait_native_ready(value)
        self.assertLessEqual(clock[0], 25)
        self.assertLess(self.manager._controller_request.call_count, 16)

    def test_wrong_fixed_node_is_fatal(self):
        states = self.states()
        states['OUTBOUND-chat'] = {'all': ['wrong'], 'now': 'wrong'}
        self.manager._controller_request = Mock(return_value={'proxies': states})
        with self.assertRaisesRegex(routing.RoutingError, '不一致'):
            self.manager._verify_business_outbounds(self.value)

    def test_probe_targets_business_groups_and_keeps_failed_node_binding(self):
        original = copy.deepcopy(self.value)
        states = self.states()
        def request(cfg, path, **kw):
            if '/delay?' in path:
                if 'OUTBOUND-chat' in path:
                    raise OSError('offline unavailable')
                return {'delay': 20}
            return states[path.split('/')[-1]]
        self.manager._controller_request = Mock(side_effect=request)
        warnings = self.manager._verify_runtime_egress(self.value)
        self.assertEqual(len(warnings), 1)
        self.assertIn('ChatGPT 专用', warnings[0])
        self.assertEqual(self.value, original)
        paths = [call.args[1] for call in self.manager._controller_request.call_args_list]
        self.assertTrue(any('/proxies/OUTBOUND-chat/delay?' in path for path in paths))
        self.assertTrue(any('/proxies/OUTBOUND-git/delay?' in path for path in paths))

    def test_late_probe_results_do_not_report_success(self):
        outbounds = {'OUTBOUND-' + item['id']: item for item in self.value['proxy_outbounds']}
        with patch.object(routing, 'bounded_calls', return_value=({}, {})):
            warnings = self.manager._probe_business_outbounds(self.value, outbounds)
        self.assertEqual(len(warnings), 2)

    def test_global_and_disabled_sets_do_not_probe_business_groups(self):
        self.value['traffic_mode'] = 'global'
        self.assertEqual(business.referenced_outbounds(self.value), [])
        self.assertEqual(self.manager._traffic_proxy_groups(self.value), [])
        self.value['traffic_mode'] = 'rule'
        for item in self.value['business_rules']:
            item['enabled'] = False
        self.assertEqual(business.referenced_outbounds(self.value), [])
        self.assertEqual(self.manager._verify_business_outbounds(self.value), [])

    def test_refresh_warns_about_all_missing_fixed_nodes_with_unicode(self):
        self.value['proxy_outbounds'][0]['name'] = '中文 e\u0301 \U0001f1ef\U0001f1f5'
        self.value['proxy_providers'][0]['selected_node'] = 'still-present'
        self.manager._controller_request = Mock(return_value={})
        self.manager.proxy_overview = Mock(return_value=[{'id': 'alpha', 'nodes': [
            {'name': '[订阅甲] still-present', 'display_name': 'still-present'}]}])
        self.manager._native_service.read_provider.return_value = {'content': b'offline'}
        with patch.object(subscription_store, 'persist_bytes_and_nodes', return_value=({}, {})):
            result = self.manager.refresh_proxy_provider(self.value, 'alpha')
        self.assertTrue(result['ok'])
        self.assertFalse(result['selection_invalid'])
        self.assertIn('中文 e\u0301 \U0001f1ef\U0001f1f5', result['warning'])
        self.assertIn('GitHub 专用', result['msg'])

    def test_service_status_respects_remaining_budget(self):
        client = routing_service.RoutingServiceClient.__new__(routing_service.RoutingServiceClient)
        client.request = Mock(return_value={})
        client.status(timeout=.25)
        self.assertEqual(client.request.call_args.kwargs['response_timeout'], .25)

    def test_apply_preserves_runtime_warnings_in_public_result(self):
        self.value['enabled'] = True
        self.manager._data_dir = self.temp.name
        self.manager._service_state = Mock(return_value={})
        self.manager._environment = Mock()
        self.manager._environment.snapshot.return_value = {'vpns': [], 'interfaces': [], 'tun_conflicts': []}
        self.manager._vpn_server_routes = Mock(return_value=([], []))
        self.manager._test_config = Mock()
        self.manager._install = Mock(return_value=['业务出口探测未通过'])
        self.manager.status = Mock(return_value={})
        with patch.object(routing, 'verify_runtime'), \
                patch.object(routing, 'validate_environment', return_value=[]), \
                patch.object(routing, 'build_mihomo_config', return_value={}), \
                patch.object(routing, 'windows_system_proxy', return_value=''):
            result = self.manager.apply(self.value)
        self.assertTrue(result['ok'])
        self.assertEqual(result['warnings'], ['业务出口探测未通过'])


if __name__ == '__main__':
    unittest.main()
