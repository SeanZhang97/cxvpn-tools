# -*- coding: utf-8 -*-
import re
import unittest
from unittest import mock

from core import routing, routing_auto_policy


def policy_config(**policy_changes):
    policy = {
        'enabled': True,
        'stages': [
            {
                'region': 'JP',
                'region_keywords': [],
                'preferred_keywords': ['高速专线', 'IPLC', 'IEPL'],
            },
            {
                'region': 'US',
                'region_keywords': [],
                'preferred_keywords': ['高速专线'],
            },
        ],
        'fallback': 'reject',
        'latency_tolerance': 20,
        'latency_tolerance_unit': 'ms',
    }
    policy.update(policy_changes)
    return routing.normalize_config({
        **routing.default_config(),
        'default_outbound': 'proxy:alpha',
        'proxy_providers': [{
            'id': 'alpha',
            'name': '订阅一',
            'url': 'https://example.test/subscription',
            'enabled': True,
            'strategy': 'url-test',
            'selection_mode': 'auto',
            'auto_policy': policy,
        }],
    })


class RoutingAutoPolicyTests(unittest.TestCase):
    def test_normalizes_ordered_regions_and_safe_keywords(self):
        config = policy_config()
        policy = config['proxy_providers'][0]['auto_policy']

        self.assertTrue(policy['enabled'])
        self.assertEqual([stage['region'] for stage in policy['stages']],
                         ['JP', 'US'])
        self.assertEqual(policy['latency_tolerance'], 20)
        self.assertEqual(policy['latency_tolerance_unit'], 'ms')
        self.assertEqual(policy['stages'][0]['selection_mode'], 'latency')
        self.assertIn('日本', routing_auto_policy.stage_region_pattern(
            policy['stages'][0]))
        self.assertIn('高速专线', routing_auto_policy.stage_preferred_pattern(
            policy['stages'][0]))
        preferred = routing_auto_policy.stage_preferred_pattern(
            policy['stages'][0])
        self.assertRegex('[订阅一] 日本东京08｜高速专线推荐', preferred)
        self.assertRegex('[订阅一] IPLC 日本东京08', preferred)
        self.assertIsNone(re.search(preferred, '[订阅一] 美国 JPG 高速专线'))

    def test_rejects_duplicate_regions_and_unbounded_tolerance(self):
        duplicate = [
            {'region': 'JP', 'preferred_keywords': []},
            {'region': 'JP', 'preferred_keywords': []},
        ]
        with self.assertRaisesRegex(routing.RoutingError, '不能重复'):
            policy_config(stages=duplicate)
        with self.assertRaisesRegex(routing.RoutingError, '0～500'):
            policy_config(latency_tolerance=501)
        with self.assertRaisesRegex(routing.RoutingError, '0～100'):
            policy_config(
                latency_tolerance=101, latency_tolerance_unit='percent')

    def test_percent_tolerance_uses_recent_matching_delay(self):
        nodes = [
            {'name': '日本东京 01', 'display_name': '日本东京 01',
             'delay': 80, 'alive': True},
            {'name': '日本东京 02', 'display_name': '日本东京 02',
             'delay': 120, 'alive': True},
            {'name': '美国洛杉矶 01', 'display_name': '美国洛杉矶 01',
             'delay': 40, 'alive': True},
        ]
        with mock.patch.object(
                routing.subscription_store, 'load_node_snapshot',
                return_value={'nodes': nodes}):
            generated = routing.build_mihomo_config(policy_config(
                latency_tolerance=20,
                latency_tolerance_unit='percent'), [])
        groups = {item['name']: item for item in generated['proxy-groups']}

        self.assertEqual(groups['AUTO-alpha-1-REGION']['tolerance'], 16)
        self.assertEqual(groups['AUTO-alpha-2-REGION']['tolerance'], 8)

    def test_failure_only_pins_node_and_never_uses_url_test_in_stage(self):
        failure_stage = [{
            'region': 'JP',
            'region_keywords': [],
            'preferred_keywords': ['高速专线'],
            'selection_mode': 'failure',
            'preferred_node': '🇯🇵 Tōkyo 日本东京 08｜高速专线',
        }]
        nodes = [{
            'name': '🇯🇵 Tōkyo 日本东京 08｜高速专线',
            'display_name': '🇯🇵 Tōkyo 日本东京 08｜高速专线',
            'delay': 86, 'alive': True,
        }]
        with mock.patch.object(
                routing.subscription_store, 'load_node_snapshot',
                return_value={'nodes': nodes}):
            generated = routing.build_mihomo_config(
                policy_config(stages=failure_stage), [])
        groups = {item['name']: item for item in generated['proxy-groups']}

        self.assertEqual(groups['AUTO-alpha-1-FALLBACK']['proxies'], [
            'AUTO-alpha-1-PINNED', 'AUTO-alpha-1-PREFERRED',
            'AUTO-alpha-1-REGION'])
        self.assertEqual(groups['AUTO-alpha-1-PINNED']['type'], 'fallback')
        self.assertEqual(groups['AUTO-alpha-1-PREFERRED']['type'], 'fallback')
        self.assertEqual(groups['AUTO-alpha-1-REGION']['type'], 'fallback')
        self.assertNotIn('tolerance', groups['AUTO-alpha-1-PINNED'])
        self.assertRegex('[订阅一] 🇯🇵 Tōkyo 日本东京 08｜高速专线',
                         groups['AUTO-alpha-1-PINNED']['filter'])

    def test_failure_only_requires_current_node_from_same_region(self):
        provider = policy_config(stages=[{
            'region': 'JP', 'selection_mode': 'failure',
            'preferred_node': '美国洛杉矶 01',
        }])['proxy_providers'][0]
        with self.assertRaisesRegex(routing.RoutingError, '不属于日本'):
            routing_auto_policy.validate_preferred_nodes(provider, [{
                'name': '美国洛杉矶 01', 'display_name': '美国洛杉矶 01',
            }], routing.RoutingError)

    def test_builds_region_then_line_then_country_failover(self):
        generated = routing.build_mihomo_config(policy_config(), [])
        groups = {item['name']: item for item in generated['proxy-groups']}

        public = groups['PROXY-alpha']
        self.assertEqual(public['type'], 'fallback')
        self.assertEqual(public['proxies'], [
            'AUTO-alpha-1-FALLBACK', 'AUTO-alpha-2-FALLBACK'])
        self.assertEqual(groups['AUTO-alpha-1-FALLBACK']['proxies'], [
            'AUTO-alpha-1-PREFERRED', 'AUTO-alpha-1-REGION'])
        self.assertEqual(groups['AUTO-alpha-1-PREFERRED']['type'], 'url-test')
        self.assertEqual(groups['AUTO-alpha-1-PREFERRED']['tolerance'], 20)
        self.assertIn('Japan', groups['AUTO-alpha-1-REGION']['filter'])
        self.assertIn('高速专线', groups['AUTO-alpha-1-PREFERRED']['filter'])
        self.assertTrue(groups['AUTO-alpha-1-PREFERRED']['hidden'])
        self.assertFalse(public['hidden'])

    def test_optional_all_nodes_fallback_is_last(self):
        generated = routing.build_mihomo_config(
            policy_config(fallback='all'), [])
        groups = {item['name']: item for item in generated['proxy-groups']}

        self.assertEqual(groups['PROXY-alpha']['proxies'][-1], 'AUTO-alpha-ALL')
        self.assertNotIn('filter', groups['AUTO-alpha-ALL'])

    def test_manual_mode_keeps_policy_dormant(self):
        config = policy_config()
        config['proxy_providers'][0]['selection_mode'] = 'manual'
        config['proxy_providers'][0]['selected_node'] = '日本东京 01'
        generated = routing.build_mihomo_config(
            routing.normalize_config(config), [])
        names = {item['name'] for item in generated['proxy-groups']}

        group = next(item for item in generated['proxy-groups']
                     if item['name'] == 'PROXY-alpha')
        self.assertEqual(group['type'], 'select')
        self.assertFalse(any(name.startswith('AUTO-alpha') for name in names))

    def test_proxy_overview_resolves_nested_selection_and_lists_real_nodes(self):
        config = policy_config()
        japan = '[订阅一] 日本东京08｜高速专线推荐'
        united_states = '[订阅一] 美国洛杉矶01'
        proxies = {
            'PROXY-alpha': {
                'all': ['AUTO-alpha-1-FALLBACK', 'AUTO-alpha-2-FALLBACK'],
                'now': 'AUTO-alpha-1-FALLBACK',
            },
            'AUTO-alpha-1-FALLBACK': {
                'all': ['AUTO-alpha-1-PREFERRED', 'AUTO-alpha-1-REGION'],
                'now': 'AUTO-alpha-1-PREFERRED',
            },
            'AUTO-alpha-1-PREFERRED': {'all': [japan], 'now': japan},
        }
        provider_payload = {'providers': {'provider-alpha': {'proxies': [
            {'name': japan, 'provider-name': 'provider-alpha', 'alive': True,
             'history': [{'delay': 86}]},
            {'name': united_states, 'provider-name': 'provider-alpha',
             'alive': True, 'history': [{'delay': 120}]},
        ]}}}
        manager = routing.RoutingManager()
        with mock.patch.object(
                manager, '_controller_request',
                side_effect=[{'proxies': proxies}, provider_payload]):
            groups = manager.proxy_overview(config)

        group = next(item for item in groups if item['id'] == 'alpha')
        self.assertEqual(group['selected'], japan)
        self.assertEqual([item['name'] for item in group['nodes']],
                         [japan, united_states])
        self.assertEqual(group['nodes'][0]['display_name'],
                         '日本东京08｜高速专线推荐')


if __name__ == '__main__':
    unittest.main()
