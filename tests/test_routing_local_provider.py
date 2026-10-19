# -*- coding: utf-8 -*-
"""本地订阅与当前业务出口的兼容边界，全部使用隔离缓存和模拟控制端。"""
import json
import os
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from core import routing, subscription_store
from core.routing_updates import RoutingUpdateWorker
from tests.test_routing_business import sample


class LocalProviderTests(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory(prefix='cxvpn-local-provider-'))
        self.enterContext(patch.dict(os.environ, {'LOCALAPPDATA': temp}))
        self.provider = routing.normalize_config({'proxy_providers': [{
            'id': 'local', 'name': '本地 e\u0301 \U0001f1ef\U0001f1f5',
            'url': '', 'enabled': True, 'auto_update': True,
        }]})['proxy_providers'][0]
        self.node = '节点 e\u0301 \U0001f1ef\U0001f1f5'
        self.payload = ('proxies:\n  - name: ' + self.node + '\n    type: direct\n').encode('utf-8')

    def test_import_and_cache_preview_do_not_update_or_claim_speedtest(self):
        manager = routing.RoutingManager()
        manager._test_config = Mock()
        process = Mock()
        process.poll.return_value = None
        process.wait.return_value = 0
        generated = []

        def start(args, **kwargs):
            with open(args[args.index('-f') + 1], encoding='utf-8') as stream:
                generated.append(json.load(stream))
            return process

        def request(config, path, **kwargs):
            self.assertNotEqual(kwargs.get('method'), 'PUT')
            if path == '/version':
                return {'version': 'offline'}
            self.assertEqual(path, '/providers/proxies/preview')
            return {'proxies': [{'name': self.node, 'type': 'direct'}]}

        with patch.object(routing, 'verify_runtime'), \
                patch.object(routing, 'windows_system_proxy') as system_proxy, \
                patch.object(routing.subprocess, 'Popen', side_effect=start), \
                patch.object(routing, '_attach_kill_on_close_job', return_value=Mock()), \
                patch.object(manager, '_controller_request', side_effect=request):
            imported = manager.import_proxy_provider(self.provider, self.payload.decode('utf-8'))
            cached = manager.preview_proxy_provider(self.provider)
        system_proxy.assert_not_called()
        self.assertEqual(imported['update_state'], 'cache_imported')
        self.assertEqual(cached['update_state'], 'cache_loaded')
        self.assertFalse(cached['tested'])
        self.assertNotIn('测速完成', cached['msg'])
        self.assertFalse(cached['refreshed'])
        self.assertEqual(cached['nodes'][0]['name'], self.node)
        self.assertTrue(subscription_store.cache_status(self.provider)['available'])
        for config in generated:
            provider = config['proxy-providers']['preview']
            self.assertEqual(provider['type'], 'file')
            self.assertNotIn('url', provider)
            self.assertFalse(provider['health-check']['enable'])

    def test_local_provider_is_not_scheduled_for_auto_update(self):
        config = routing.normalize_config({'proxy_providers': [self.provider]})
        manager = Mock()
        worker = RoutingUpdateWorker(lambda: {'routing': config}, manager, threading.Lock())
        with patch.object(subscription_store, 'cache_status', return_value={'updated_at': 0}):
            self.assertFalse(worker._update_one_due_provider())
        manager.status.assert_not_called()
        manager.preview_proxy_provider.assert_not_called()
        manager.refresh_proxy_provider.assert_not_called()
        self.assertEqual(worker._attempts, {})

    def test_local_parse_failure_does_not_retry_remote_routes(self):
        manager = routing.RoutingManager()
        with patch.object(routing, 'windows_system_proxy') as system_proxy, \
                patch.object(manager, '_preview_proxy_provider_once',
                             side_effect=routing.ProviderFetchError('invalid cache')) as preview:
            with self.assertRaises(routing.ProviderFetchError):
                manager.preview_proxy_provider(self.provider)
        system_proxy.assert_not_called()
        preview.assert_called_once()

    def test_business_only_egress_preserves_probe_and_warnings(self):
        config = sample()
        manager = routing.RoutingManager()
        states = {'OUTBOUND-' + item['id']: {
                      'now': '[订阅甲] ' + item['node_name'],
                      'all': ['[订阅甲] ' + item['node_name']]}
                  for item in config['proxy_outbounds']}
        manager._controller_request = Mock(side_effect=lambda cfg, path, **kw: states[path.split('/')[-1]])
        manager._probe_business_outbounds = Mock(return_value=['独立出口探测警告'])
        manager._probe_connectivity = Mock()
        with patch.object(routing.urllib.request, 'build_opener'):
            for capture in ('tun', 'system-proxy'):
                config['capture_mode'] = capture
                self.assertEqual(manager._verify_runtime_egress(config), ['独立出口探测警告'])
                self.assertEqual(set(manager._probe_connectivity.call_args.args[2]), set(states))
        self.assertEqual(manager._probe_business_outbounds.call_count, 2)


if __name__ == '__main__':
    unittest.main()
