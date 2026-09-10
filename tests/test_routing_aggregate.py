# -*- coding: utf-8 -*-
"""聚合出口独立选点的离线配置、提交和运行确认边界。"""
import copy
import json
import os
import re
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from api import Api
from core import routing, routing_aggregate as aggregate, routing_selection, subscription_store


NODE = '中文 e\u0301 \U0001f1ef\U0001f1f5 [a].+?(x)|$^\\'


def config(manual=True):
    return routing.normalize_config({
        **routing.default_config(), 'default_outbound': 'proxy',
        'proxy_providers': [{
            'id': key, 'name': name, 'url': 'https://example.test/sub',
            'enabled': True, 'selection_mode': 'manual', 'selected_node': '订阅独立节点',
        } for key, name in [('alpha', '订阅甲'), ('beta', '订阅乙')]],
        'aggregate_selection': {'mode': 'manual', 'provider_id': 'beta', 'node_name': NODE}
        if manual else aggregate.default_selection(),
    })


def instance(current):
    api = Api.__new__(Api)
    api.cfg = {'routing': copy.deepcopy(current)}
    api._lock = threading.Lock()
    api._routing_lock = threading.Lock()
    api.routing = Mock()
    api.routing.status.return_value = {'running': False, 'core_running': True}
    api._poke_ui_state = Mock()
    api.log = Mock()
    return api


class AggregateSelectionTests(unittest.TestCase):
    def test_old_config_keeps_strategy_and_gets_auto_default(self):
        old = config(False)
        old.pop('aggregate_selection')
        old['schema_version'] = 7
        old['proxy_strategy'] = 'fallback'
        normalized = routing.normalize_config(old)
        self.assertEqual(normalized['schema_version'], 8)
        self.assertEqual(normalized['proxy_strategy'], 'fallback')
        self.assertEqual(normalized['aggregate_selection'], aggregate.default_selection())

    def test_manual_group_is_exact_leaf_and_fails_closed(self):
        value = config()
        generated = routing.build_mihomo_config(value, [])
        group = next(item for item in generated['proxy-groups'] if item['name'] == 'PROXY')
        self.assertEqual(group['use'], ['provider-beta'])
        self.assertEqual(group['type'], 'select')
        self.assertEqual(group['empty-fallback'], 'REJECT')
        self.assertNotIn('proxies', group)
        self.assertTrue(re.fullmatch(group['filter'], '[订阅乙] ' + NODE))
        self.assertIsNone(re.fullmatch(group['filter'], '[订阅甲] ' + NODE))
        self.assertIsNone(re.fullmatch(group['filter'], '[订阅乙] ' + NODE + '其它'))
        self.assertIsNone(re.fullmatch(group['filter'], 'DIRECT'))
        self.assertEqual(value['proxy_providers'][1]['selected_node'], '订阅独立节点')

    def test_auto_group_keeps_subscription_layering(self):
        generated = routing.build_mihomo_config(config(False), [])
        group = next(item for item in generated['proxy-groups'] if item['name'] == 'PROXY')
        self.assertEqual(group['proxies'], ['PROXY-alpha', 'PROXY-beta'])
        self.assertNotIn('use', group)

    def test_manual_aggregate_does_not_validate_unused_subscription_preferences(self):
        value = config()
        self.assertEqual(routing._referenced_provider_ids(value), set())
        self.assertEqual(routing._referenced_group_ids(value), {'all'})
        with patch.object(subscription_store, 'load_node_snapshot', return_value={'nodes': [{'name': NODE}]}):
            self.assertEqual(routing_selection.manual_selection_error(value, {'all'}), '')
        self.assertEqual(routing_selection.manual_runtime_targets(value, {'all'}), [('all', '[订阅乙] ' + NODE)])
        value['rules'] = [{'enabled': True, 'outbound': 'proxy:alpha'}]
        self.assertEqual(routing._referenced_group_ids(value), {'all', 'alpha'})

    def test_disabled_deleted_or_invalid_owner_is_rejected(self):
        for owner in ('missing', 'beta'):
            value = config()
            value['aggregate_selection']['provider_id'] = owner
            value['proxy_providers'][1]['enabled'] = False
            with self.subTest(owner=owner), self.assertRaisesRegex(routing.RoutingError, '固定节点所属订阅'):
                routing.normalize_config(value)
        value = config()
        value['aggregate_selection']['node_name'] = 'bad\nnode'
        with self.assertRaises(routing.RoutingError):
            routing.normalize_config(value)

    def test_missing_node_blocks_start_but_not_switching_back_to_auto(self):
        value = config()
        with patch.object(subscription_store, 'load_node_snapshot', return_value={'nodes': []}):
            self.assertIn('固定节点已失效', routing_selection.manual_selection_error(value, {'all'}))
        api = instance(value)
        with patch('core.routing_aggregate.cfgmod.save'):
            result = api.save_aggregate_proxy_preference('auto')
        self.assertTrue(result['ok'])
        self.assertEqual(api.cfg['routing']['aggregate_selection']['mode'], 'auto')

    def test_save_offline_round_trip_preserves_independent_preferences(self):
        value = config(False)
        api = instance(value)
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'LOCALAPPDATA': root}), \
                patch('core.routing_aggregate.cfgmod.save') as save:
            subscription_store.persist_node_snapshot(value['proxy_providers'][1], [{'name': NODE}])
            result = api.save_aggregate_proxy_preference('manual', 'beta', NODE)
        self.assertTrue(result['ok'])
        self.assertTrue(result['requires_apply'])
        self.assertEqual(result['config']['proxy_providers'], value['proxy_providers'])
        self.assertEqual(result['config']['default_outbound'], 'proxy')
        saved = json.loads(json.dumps(save.call_args.args[0], ensure_ascii=False))
        self.assertEqual(routing.normalize_config(saved['routing'])['aggregate_selection']['node_name'], NODE)
        api.routing.select_proxy_node.assert_not_called()

    def test_invalid_request_and_failed_disk_save_preserve_config(self):
        for mode, owner, nodes, save_error in [
            ('bad', 'beta', [], None), ('manual', 'missing', [], None),
            ('manual', 'beta', [], None), ('manual', 'beta', [{'name': NODE}], OSError('disk full')),
        ]:
            api = instance(config(False))
            old = copy.deepcopy(api.cfg)
            with self.subTest(mode=mode, owner=owner, save_error=bool(save_error)), \
                    patch.object(subscription_store, 'load_node_snapshot', return_value={'nodes': nodes}), \
                    patch('core.routing_aggregate.cfgmod.save', side_effect=save_error):
                result = api.save_aggregate_proxy_preference(mode, owner, NODE)
            self.assertFalse(result['ok'])
            self.assertEqual(api.cfg, old)

    def test_active_selection_reuses_transaction_and_reports_failure(self):
        for ok in (True, False):
            api = instance(config(False))
            api.routing.status.return_value = {'running': True}
            api.routing.proxy_overview.return_value = []
            api._apply_routing_locked = Mock(return_value={'ok': ok, 'config': config(), 'msg': 'result'})
            with patch.object(subscription_store, 'load_node_snapshot', return_value={'nodes': [{'name': NODE}]}), \
                    patch('core.routing_aggregate.cfgmod.save') as save:
                result = api.save_aggregate_proxy_preference('manual', 'beta', NODE)
            self.assertEqual(result['ok'], ok)
            api._apply_routing_locked.assert_called_once()
            self.assertEqual(api._apply_routing_locked.call_args.args[0]['aggregate_selection']['node_name'], NODE)
            save.assert_not_called()

    def test_native_ready_confirms_aggregate_without_touching_provider_groups(self):
        value = config()
        target = aggregate.runtime_target(value)
        manager = routing.RoutingManager()
        manager.log = Mock()
        manager._controller_version = Mock(return_value=True)
        manager._controller_request = Mock(side_effect=[{'all': [target]}, {}, {'now': target}])
        manager._verify_runtime_egress = Mock()
        manager._wait_native_ready(value)
        self.assertTrue(all(call.args[1] == '/proxies/PROXY' for call in manager._controller_request.call_args_list))
        self.assertEqual(manager._controller_request.call_args_list[1].kwargs['payload'], {'name': target})

    def test_aggregate_readback_mismatch_is_not_success(self):
        value = config()
        target = aggregate.runtime_target(value)
        manager = routing.RoutingManager()
        manager.log = Mock()
        manager._controller_version = Mock(return_value=True)
        manager._controller_request = Mock(side_effect=[{'all': [target]}, {}, {'now': 'REJECT'}])
        manager._verify_runtime_egress = Mock()
        with self.assertRaises(routing.RoutingError):
            manager._wait_native_ready(value)
        manager._verify_runtime_egress.assert_not_called()


if __name__ == '__main__':
    unittest.main()
