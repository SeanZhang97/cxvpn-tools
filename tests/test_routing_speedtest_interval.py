# -*- coding: utf-8 -*-
"""订阅测速周期、更新周期与持久化的独立性。"""
import json
import os
import tempfile
import unittest
from unittest import mock

from core import config as cfgmod
from core import routing, subscription_store


def provider(provider_id='alpha', **changes):
    return {
        'id': provider_id, 'name': f'订阅 {provider_id} e\u0301 \U0001f1ef\U0001f1f5',
        'url': f'https://example.test/{provider_id}', 'download_route': 'physical',
        **changes,
    }


def config_for(*providers):
    return routing.normalize_config({
        **routing.default_config(), 'default_outbound': 'proxy',
        'proxy_providers': list(providers),
    })


class SpeedtestIntervalTests(unittest.TestCase):
    def test_legacy_defaults_preserve_subscription_update_settings(self):
        current = config_for(provider(interval=7200, auto_update=False))['proxy_providers'][0]
        self.assertEqual(current['speedtest_interval'], 300)
        self.assertEqual(current['interval'], 7200)
        self.assertFalse(current['auto_update'])

    def test_range_and_whole_minutes_are_validated(self):
        for value in (60, 300, '600', 86400):
            with self.subTest(valid=value):
                current = config_for(provider(speedtest_interval=value))
                self.assertEqual(current['proxy_providers'][0]['speedtest_interval'], int(value))
        for value in (0, -60, 59, 90, 86460, '', None, True, 60.5, 'nan'):
            with self.subTest(invalid=value), self.assertRaisesRegex(
                    routing.RoutingError, '自动测速间隔必须为 1～1440 的整数分钟'):
                config_for(provider(speedtest_interval=value))

    def test_each_provider_and_aggregate_use_their_own_schedule(self):
        current = config_for(
            provider(speedtest_interval=600, interval=1800, auto_update=True),
            provider('beta', speedtest_interval=1200, interval=7200, strategy='fallback'),
            provider('disabled', speedtest_interval=60, enabled=False))
        for standby in (False, True):
            with self.subTest(standby=standby):
                generated = routing.build_mihomo_config(current, [], standby=standby)
                sources = generated['proxy-providers']
                self.assertEqual(len(sources), 2)
                for source in sources.values():
                    expected = 600 if source['url'].endswith('/alpha') else 1200
                    self.assertEqual(source['health-check']['interval'], expected)
                    self.assertTrue(source['health-check']['lazy'])
                    if expected == 600:
                        self.assertEqual(source['interval'], 1800)
                    else:
                        self.assertNotIn('interval', source)
                groups = {item['name']: item for item in generated['proxy-groups']}
                self.assertEqual(groups['PROXY-alpha']['interval'], 600)
                self.assertEqual(groups['PROXY-beta']['interval'], 1200)
                self.assertEqual(groups['PROXY']['interval'], 600)

    def test_smart_policy_and_download_fallback_share_provider_interval(self):
        current = config_for(provider(speedtest_interval=900, auto_policy={
            'enabled': True, 'fallback': 'all', 'stages': [
                {'region': 'JP', 'selection_mode': 'failure',
                 'preferred_node': '日本 e\u0301 \U0001f1ef\U0001f1f5',
                 'preferred_keywords': ['专线']},
                {'region': 'US', 'preferred_keywords': ['专线']},
            ],
        }))
        for download in (
                {'target': 'DOWNLOAD-alpha', 'proxy_url': '', 'self_bootstrap': True},
                {'target': 'DOWNLOAD-alpha', 'proxy_url': 'http://127.0.0.1:7890',
                 'upstream': 'DOWNLOAD-UPSTREAM-alpha'}):
            with self.subTest(download=download['target']), \
                    mock.patch.object(routing, '_provider_download_route', return_value=download), \
                    mock.patch.object(subscription_store, 'load_node_snapshot', return_value={}):
                generated = routing.build_mihomo_config(current, [])
            groups = generated['proxy-groups']
            self.assertGreater(len(groups), 6)
            for group in groups:
                self.assertEqual(group['interval'], 900, group['name'])

    def test_manual_selection_keeps_background_provider_healthcheck(self):
        current = config_for(provider(speedtest_interval=1800, selection_mode='manual'))
        generated = routing.build_mihomo_config(current, [])
        group = next(item for item in generated['proxy-groups'] if item['name'] == 'PROXY-alpha')
        self.assertEqual(group['type'], 'select')
        self.assertNotIn('interval', group)
        source = next(iter(generated['proxy-providers'].values()))
        self.assertEqual(source['health-check']['interval'], 1800)

    def test_saved_config_and_utf8_export_keep_independent_values(self):
        current = config_for(provider(speedtest_interval=1200, interval=5400))
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(cfgmod, 'CFG_PATH', os.path.join(root, 'config.json')), \
                mock.patch.object(cfgmod, 'CFG_HISTORY_DIR', os.path.join(root, 'history')):
            cfgmod.save({'routing': current})
            loaded = cfgmod.load()['routing']
            with open(cfgmod.CFG_PATH, encoding='utf-8') as stream:
                exported = json.load(stream)['routing']
        self.assertEqual(loaded['proxy_providers'], current['proxy_providers'])
        self.assertEqual(exported['proxy_providers'], current['proxy_providers'])

    def test_interval_change_invalidates_runtime_signature_but_not_node_cache(self):
        before = config_for(provider())
        after = config_for(provider(speedtest_interval=600))
        self.assertNotEqual(routing.RoutingManager._config_signature(before),
                            routing.RoutingManager._config_signature(after))
        self.assertEqual(subscription_store.snapshot_key(before['proxy_providers'][0]),
                         subscription_store.snapshot_key(after['proxy_providers'][0]))


if __name__ == '__main__':
    unittest.main()
