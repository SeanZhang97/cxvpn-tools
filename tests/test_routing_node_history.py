# -*- coding: utf-8 -*-
"""选点后的运行目录必须保留同一订阅的最近测速结果。"""
import copy
import json
import os
import tempfile
import threading
import unittest
from unittest import mock

from api import Api
from core import routing, subscription_store


class RoutingNodeHistoryTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory(prefix='cxvpn-node-history-')
        self.addCleanup(root.cleanup)
        environment = mock.patch.dict(os.environ, {'LOCALAPPDATA': root.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.config = routing.normalize_config({
            'enabled': True, 'default_outbound': 'proxy',
            'proxy_providers': [
                {'id': key, 'name': name, 'url': f'https://example.test/{key}',
                 'enabled': True, 'selection_mode': 'manual', 'selected_node': '节点 e\u0301 \U0001f1ef\U0001f1f5'}
                for key, name in [('alpha', '订阅甲 e\u0301 \U0001f1ef\U0001f1f5'), ('beta', '订阅乙')]
            ],
        })
        self.raw_names = ['节点 e\u0301 \U0001f1ef\U0001f1f5', '节点二', '已移除节点']
        self.snapshots = {}
        self.proxies = {'PROXY': {'all': ['PROXY-alpha', 'PROXY-beta'], 'now': 'PROXY-alpha'}}
        for index, provider in enumerate(self.config['proxy_providers']):
            nodes = [{'name': name, 'display_name': name, 'type': 'Vless',
                      'delay': 30 + index * 100 + offset, 'alive': True,
                      'tested': True, 'tested_at': 1_800_000_000}
                     for offset, name in enumerate(self.raw_names)]
            self.snapshots[provider['id']] = subscription_store.persist_node_snapshot(provider, nodes)
            names = [f'[{provider["name"]}] {name}' for name in self.raw_names[:2]]
            self.proxies[f'PROXY-{provider["id"]}'] = {'all': names, 'now': names[0]}
            for name in names:
                self.proxies[name] = {'type': 'Vless', 'alive': True, 'history': []}
        self.manager = routing.RoutingManager(logger=mock.Mock())
        self.manager._controller_request = mock.Mock(side_effect=self.controller)
        self.manager.status = mock.Mock(return_value={'running': True, 'core_running': True})
        self.manager.remember_selection = mock.Mock()

    def controller(self, _config, path, method='GET', payload=None, **_kwargs):
        if path == '/proxies':
            return {'proxies': copy.deepcopy(self.proxies)}
        if path == '/providers/proxies':
            return {'providers': {}}
        if path in ('/proxies/PROXY-alpha', '/proxies/PROXY-beta'):
            group = self.proxies[path.rsplit('/', 1)[1]]
            if method == 'PUT':
                group['now'] = payload['name']
            return copy.deepcopy(group)
        raise AssertionError(f'未模拟 Controller 路径: {path}')

    def instance(self, config=None):
        instance = Api.__new__(Api)
        instance._lock = threading.Lock()
        instance._routing_lock = threading.Lock()
        instance.cfg = {'routing': copy.deepcopy(config or self.config)}
        instance.routing = self.manager
        instance.log = mock.Mock()
        return instance

    def assert_history(self, groups):
        groups = {group['id']: group for group in groups}
        for index, provider in enumerate(self.config['proxy_providers']):
            group = groups[provider['id']]
            self.assertEqual(len(group['nodes']), 2, '移除的节点不能由历史记录复活')
            self.assertEqual(group['alive_count'], 2)
            for offset, node in enumerate(group['nodes']):
                self.assertTrue(node['tested'])
                self.assertEqual(node['tested_at'], 1_800_000_000)
                self.assertEqual(node['delay'], 30 + index * 100 + offset)
                aggregate = next(item for item in groups['all']['nodes'] if item['name'] == node['name'])
                self.assertEqual(aggregate['delay'], node['delay'])
                self.assertTrue(aggregate['tested'])

    def test_manual_selection_response_and_refresh_keep_other_node_history(self):
        instance = self.instance()
        with mock.patch('api.cfgmod.save'):
            result = instance.save_proxy_preference('alpha', 'manual', self.raw_names[1])
        self.assertTrue(result['ok'], result.get('msg'))
        self.assert_history(json.loads(json.dumps(result, ensure_ascii=False))['groups'])
        self.assert_history(self.manager.proxy_overview(instance.cfg['routing']))
        selected = next(group for group in result['groups'] if group['id'] == 'alpha')['selected']
        self.assertTrue(selected.endswith(self.raw_names[1]))
        for provider in self.config['proxy_providers']:
            self.assertEqual(subscription_store.load_node_snapshot(provider), self.snapshots[provider['id']])

    def test_auto_to_manual_and_aggregate_reload_responses_keep_history(self):
        for aggregate in (False, True):
            with self.subTest(aggregate=aggregate):
                config = copy.deepcopy(self.config)
                config['proxy_providers'][0]['selection_mode'] = 'auto'
                instance = self.instance(config)
                def apply(candidate, _source):
                    return {'ok': True, 'config': candidate, 'status': {'running': True}}
                with mock.patch.object(instance, '_apply_routing_locked', side_effect=apply):
                    result = (instance.save_aggregate_proxy_preference('manual', 'alpha', self.raw_names[1])
                              if aggregate else instance.save_proxy_preference('alpha', 'manual', self.raw_names[1]))
                self.assertTrue(result['ok'], result.get('msg'))
                self.assert_history(result['groups'])

    def test_newer_success_and_failure_replace_history_but_older_do_not(self):
        provider = self.config['proxy_providers'][0]
        name = f'[{provider["name"]}] {self.raw_names[0]}'
        for stamp, delay, alive, expected in [
                ('2027-01-15T08:00:01Z', 15, True, 15),
                ('2027-01-15T08:00:01Z', 0, False, 0),
                ('2027-01-15T07:59:59Z', 15, True, 30)]:
            with self.subTest(stamp=stamp, delay=delay):
                self.proxies[name].update({'history': [{'time': stamp, 'delay': delay}], 'alive': alive})
                groups = self.manager.proxy_overview(self.config)
                node = next(group for group in groups if group['id'] == 'alpha')['nodes'][0]
                self.assertTrue(node['tested'])
                self.assertEqual(node['delay'], expected)
                self.assertEqual(node['alive'], expected > 0)
                self.assertEqual(node['tested_at'], 1_800_000_001 if expected != 30 else 1_800_000_000)

    def test_new_nodes_and_changed_subscription_identity_do_not_inherit_tests(self):
        for change in ({'url': 'https://example.test/new'}, {'filter': '新'},
                       {'exclude_filter': '旧'}, {'id': 'other'}):
            with self.subTest(change=change):
                config = copy.deepcopy(self.config)
                config['proxy_providers'][0].update(change)
                groups = self.manager.proxy_overview(config)
                node = next(group for group in groups if group['id'] == 'all')['nodes'][0]
                self.assertFalse(node['tested'])
                self.assertEqual(node['tested_at'], 0)
        name = f'[{self.config["proxy_providers"][0]["name"]}] 新节点'
        self.proxies['PROXY-alpha']['all'].append(name)
        self.proxies[name] = {'type': 'Vless', 'history': []}
        groups = self.manager.proxy_overview(self.config)
        node = next(group for group in groups if group['id'] == 'alpha')['nodes'][-1]
        self.assertFalse(node['tested'])
        self.assertEqual(node['tested_at'], 0)

    def test_snapshot_read_failure_keeps_live_result_and_records_phase(self):
        with mock.patch.object(subscription_store, 'load_node_snapshot', side_effect=OSError('unavailable')):
            groups = self.manager.proxy_overview(self.config)
        self.assertEqual(len(groups), 3)
        self.assertFalse(groups[1]['nodes'][0]['tested'])
        self.assertTrue(any('测速历史读取失败' in call.args[0] for call in self.manager.log.call_args_list))

    def test_last_failed_test_is_not_changed_by_default_alive_flag(self):
        provider = self.config['proxy_providers'][0]
        nodes = copy.deepcopy(self.snapshots['alpha']['nodes'])
        nodes[0].update({'delay': 0, 'alive': False, 'tested_at': 1_800_000_001})
        subscription_store.persist_node_snapshot(provider, nodes)
        groups = {group['id']: group for group in self.manager.proxy_overview(self.config)}
        self.assertTrue(groups['alpha']['nodes'][0]['tested'])
        self.assertFalse(groups['alpha']['nodes'][0]['alive'])
        self.assertEqual(groups['alpha']['alive_count'], 1)
        self.assertEqual(groups['all']['alive_count'], 3)

    def test_config_save_failure_rolls_back_selection_without_erasing_history(self):
        instance = self.instance()
        original = self.proxies['PROXY-alpha']['now']
        with mock.patch('api.cfgmod.save', side_effect=OSError('disk unavailable')):
            result = instance.save_proxy_preference('alpha', 'manual', self.raw_names[1])
        self.assertFalse(result['ok'])
        self.assertEqual(self.proxies['PROXY-alpha']['now'], original)
        self.assertEqual(instance.cfg['routing'], self.config)
        self.assert_history(self.manager.proxy_overview(self.config))


if __name__ == '__main__':
    unittest.main()
