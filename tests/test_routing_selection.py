# -*- coding: utf-8 -*-
import os
import tempfile
import threading
import unittest
from unittest import mock

from api import Api
from core import routing, routing_selection, subscription_store


def provider_config(**changes):
    provider = {
        'id': 'alpha', 'name': '订阅一',
        'url': 'https://example.test/subscription',
        'enabled': True, 'strategy': 'url-test',
    }
    provider.update(changes)
    return {
        'enabled': False,
        'proxy_providers': [provider],
        'default_outbound': 'proxy:alpha',
        'controller_secret': 'test-secret',
    }


class RoutingSelectionTests(unittest.TestCase):
    def test_unused_manual_provider_is_not_validated_or_applied(self):
        config = routing.normalize_config(provider_config(
            selection_mode='manual', selected_node='Removed Node'))
        config['default_outbound'] = 'physical'

        targets = routing._referenced_provider_ids(config)

        self.assertEqual(targets, set())
        self.assertEqual(
            routing_selection.manual_selection_error(config, targets), '')
        self.assertEqual(
            routing_selection.manual_runtime_targets(config, targets), [])

    def test_apply_allows_unused_manual_provider_without_snapshot(self):
        config = routing.normalize_config(provider_config(
            selection_mode='manual', selected_node='Removed Node'))
        config['enabled'] = True
        config['default_outbound'] = 'physical'
        manager = routing.RoutingManager()
        with mock.patch.object(routing, 'verify_runtime'), \
                mock.patch.object(routing.vpn_os, 'list_vpns', return_value=[]), \
                mock.patch.object(routing, 'list_physical_interfaces',
                                  return_value=[{'name': '以太网'}]), \
                mock.patch.object(routing, 'list_tun_conflicts', return_value=[]), \
                mock.patch.object(manager, '_test_config'), \
                mock.patch.object(manager, '_install') as install, \
                mock.patch.object(manager, 'status', return_value={'running': True}):
            result = manager.apply(config)

        self.assertTrue(result['ok'])
        install.assert_called_once()

    def test_aggregate_proxy_references_every_enabled_provider(self):
        config = routing.normalize_config(provider_config())
        config['default_outbound'] = 'proxy'

        self.assertEqual(routing._referenced_provider_ids(config), {'alpha'})

    def test_manual_mode_builds_select_group_and_auto_update_is_opt_in(self):
        config = routing.normalize_config(provider_config(
            selection_mode='manual', selected_node='Hong Kong 01'))

        generated = routing.build_mihomo_config(config, [])
        group = next(item for item in generated['proxy-groups']
                     if item['name'] == 'PROXY-alpha')
        provider = generated['proxy-providers']['provider-alpha']

        self.assertEqual(group['type'], 'select')
        self.assertNotIn('interval', provider)

        enabled = routing.normalize_config(provider_config(auto_update=True))
        generated = routing.build_mihomo_config(enabled, [])
        self.assertEqual(
            generated['proxy-providers']['provider-alpha']['interval'], 3600)

    def test_removed_manual_node_is_reported_before_start(self):
        config = routing.normalize_config(provider_config(
            selection_mode='manual', selected_node='Removed Node'))
        provider = config['proxy_providers'][0]
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.dict(os.environ, {'LOCALAPPDATA': root}):
            subscription_store.persist_node_snapshot(provider, [{
                'name': 'Current Node', 'display_name': 'Current Node',
                'type': 'vmess', 'delay': None, 'alive': None,
                'tested': False, 'tested_at': 0,
            }])
            message = routing_selection.manual_selection_error(config)

        self.assertIn('已失效', message)

    def test_runtime_refresh_reports_removed_selected_node(self):
        config = routing.normalize_config(provider_config(
            enabled=True, selection_mode='manual',
            selected_node='Removed Node'))
        runtime_name = '[订阅一] Current Node'
        payload = {'proxies': {
            'PROXY-alpha': {'all': [runtime_name], 'now': runtime_name},
            runtime_name: {'alive': True, 'history': [{'delay': 30}]},
        }}
        manager = routing.RoutingManager()
        with mock.patch.object(
                manager, '_controller_request',
                side_effect=[{}, payload, {'providers': {}}]), \
                mock.patch.object(
                    routing_selection, 'persist_provider_nodes'):
            result = manager.refresh_proxy_provider(config, 'alpha')

        self.assertTrue(result['selection_invalid'])
        self.assertIn('失效', result['msg'])

    @mock.patch('api.cfgmod.save')
    def test_api_persists_selected_node_for_next_start(self, save):
        current = routing.normalize_config(provider_config())
        instance = Api.__new__(Api)
        instance._lock = threading.Lock()
        instance._routing_lock = threading.Lock()
        instance.cfg = {'routing': current}
        instance.routing = mock.Mock()
        instance.routing.status.return_value = {'running': False}
        instance.log = mock.Mock()
        provider = current['proxy_providers'][0]

        with tempfile.TemporaryDirectory() as root, \
                mock.patch.dict(os.environ, {'LOCALAPPDATA': root}):
            subscription_store.persist_node_snapshot(provider, [{
                'name': 'Tokyo 01', 'display_name': 'Tokyo 01',
                'type': 'vless', 'delay': 48, 'alive': True,
                'tested': True, 'tested_at': 1,
            }])
            result = instance.save_proxy_preference(
                'alpha', 'manual', 'Tokyo 01')

        self.assertTrue(result['ok'])
        selected = instance.cfg['routing']['proxy_providers'][0]
        self.assertEqual(selected['selection_mode'], 'manual')
        self.assertEqual(selected['selected_node'], 'Tokyo 01')
        self.assertEqual(instance.cfg['routing']['default_outbound'],
                         'proxy:alpha')
        save.assert_called_once()

    @mock.patch('api.cfgmod.save')
    def test_api_persists_smart_auto_policy(self, save):
        current = routing.normalize_config(provider_config())
        instance = Api.__new__(Api)
        instance._lock = threading.Lock()
        instance._routing_lock = threading.Lock()
        instance.cfg = {'routing': current}
        instance.routing = mock.Mock()
        instance.routing.status.return_value = {'running': False}
        instance.log = mock.Mock()
        policy = {
            'enabled': True,
            'stages': [
                {'region': 'JP', 'preferred_keywords': ['高速专线']},
                {'region': 'US', 'preferred_keywords': ['高速专线']},
            ],
            'fallback': 'reject',
            'latency_tolerance': 20,
        }

        result = instance.save_proxy_preference(
            'alpha', 'auto', '', None, policy)

        self.assertTrue(result['ok'])
        stored = instance.cfg['routing']['proxy_providers'][0]
        self.assertTrue(stored['auto_policy']['enabled'])
        self.assertEqual(
            [item['region'] for item in stored['auto_policy']['stages']],
            ['JP', 'US'])
        self.assertEqual(instance.cfg['routing']['default_outbound'],
                         'proxy:alpha')
        save.assert_called_once()

    @mock.patch('api.cfgmod.save')
    def test_api_persists_failure_only_preferred_node(self, save):
        current = routing.normalize_config(provider_config())
        instance = Api.__new__(Api)
        instance._lock = threading.Lock()
        instance._routing_lock = threading.Lock()
        instance.cfg = {'routing': current}
        instance.routing = mock.Mock()
        instance.routing.status.return_value = {'running': False}
        instance.log = mock.Mock()
        provider = current['proxy_providers'][0]
        policy = {
            'enabled': True,
            'stages': [{
                'region': 'JP', 'selection_mode': 'failure',
                'preferred_node': '日本东京 08｜高速专线',
                'preferred_keywords': ['高速专线'],
            }],
            'fallback': 'reject',
            'latency_tolerance': 20,
            'latency_tolerance_unit': 'percent',
        }

        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
                os.environ, {'LOCALAPPDATA': root}):
            subscription_store.persist_node_snapshot(provider, [{
                'name': '日本东京 08｜高速专线',
                'display_name': '日本东京 08｜高速专线',
                'type': 'vless', 'delay': 86, 'alive': True,
                'tested': True, 'tested_at': 1,
            }])
            result = instance.save_proxy_preference(
                'alpha', 'auto', '', None, policy)

        self.assertTrue(result['ok'])
        stored = instance.cfg['routing']['proxy_providers'][0]['auto_policy']
        self.assertEqual(stored['stages'][0]['selection_mode'], 'failure')
        self.assertEqual(stored['stages'][0]['preferred_node'],
                         '日本东京 08｜高速专线')
        self.assertEqual(stored['latency_tolerance_unit'], 'percent')
        save.assert_called_once()

    @mock.patch('api.cfgmod.save')
    def test_running_smart_policy_change_reuses_apply_transaction(self, save):
        current = routing.normalize_config(provider_config())
        instance = Api.__new__(Api)
        instance._lock = threading.Lock()
        instance._routing_lock = threading.Lock()
        instance.cfg = {'routing': current}
        instance.routing = mock.Mock()
        instance.routing.status.return_value = {'running': True}
        instance.log = mock.Mock()
        policy = {
            'enabled': True,
            'stages': [{'region': 'JP', 'preferred_keywords': ['高速专线']}],
            'fallback': 'reject',
            'latency_tolerance': 20,
        }
        expected = routing.normalize_config({
            **current,
            'proxy_providers': [{
                **current['proxy_providers'][0], 'auto_policy': policy,
            }],
        })

        with mock.patch.object(
                instance, '_apply_routing_locked', return_value={
                    'ok': True, 'config': expected, 'status': {'running': True},
                }) as apply_locked:
            result = instance.save_proxy_preference(
                'alpha', 'auto', '', None, policy)

        self.assertTrue(result['ok'])
        self.assertFalse(result['requires_apply'])
        self.assertIn('立即应用', result['msg'])
        apply_locked.assert_called_once()
        save.assert_not_called()

    @mock.patch('api.cfgmod.save')
    def test_api_saves_preview_provider_without_changing_route_when_selecting_node(
            self, save):
        current = routing.normalize_config({
            'enabled': False,
            'proxy_providers': [],
            'default_outbound': 'physical',
        })
        draft = provider_config(enabled=False)['proxy_providers'][0]
        instance = Api.__new__(Api)
        instance._lock = threading.Lock()
        instance._routing_lock = threading.Lock()
        instance.cfg = {'routing': current}
        instance.routing = mock.Mock()
        instance.routing.status.return_value = {'running': False}
        instance.log = mock.Mock()

        with tempfile.TemporaryDirectory() as root, \
                mock.patch.dict(os.environ, {'LOCALAPPDATA': root}):
            subscription_store.persist_node_snapshot(draft, [{
                'name': 'Tokyo 01', 'display_name': 'Tokyo 01',
                'type': 'vless', 'delay': 48, 'alive': True,
                'tested': True, 'tested_at': 1,
            }])
            result = instance.save_proxy_preference(
                'alpha', 'manual', 'Tokyo 01', draft)

        self.assertTrue(result['ok'])
        provider = result['config']['proxy_providers'][0]
        self.assertFalse(provider['enabled'])
        self.assertEqual(provider['selection_mode'], 'manual')
        self.assertEqual(provider['selected_node'], 'Tokyo 01')
        self.assertEqual(result['config']['default_outbound'], 'physical')
        self.assertIn('启用该订阅', result['msg'])
        save.assert_called_once()

    @mock.patch('api.cfgmod.save')
    def test_manual_preference_does_not_change_existing_default_outbound(
            self, save):
        current = routing.normalize_config(provider_config())
        current['default_outbound'] = 'physical'
        instance = Api.__new__(Api)
        instance._lock = threading.Lock()
        instance._routing_lock = threading.Lock()
        instance.cfg = {'routing': current}
        instance.routing = mock.Mock()
        instance.routing.status.return_value = {'running': False}
        instance.log = mock.Mock()
        provider = current['proxy_providers'][0]

        with tempfile.TemporaryDirectory() as root, \
                mock.patch.dict(os.environ, {'LOCALAPPDATA': root}):
            subscription_store.persist_node_snapshot(provider, [{
                'name': 'Tokyo 01', 'display_name': 'Tokyo 01',
                'type': 'vless', 'delay': 48, 'alive': True,
                'tested': True, 'tested_at': 1,
            }])
            result = instance.save_proxy_preference(
                'alpha', 'manual', 'Tokyo 01')

        self.assertTrue(result['ok'])
        self.assertEqual(result['config']['default_outbound'], 'physical')


if __name__ == '__main__':
    unittest.main()
