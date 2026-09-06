# -*- coding: utf-8 -*-
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import routing, subscription_store


ROOT = Path(__file__).resolve().parents[1]
MIHOMO = ROOT / 'runtime' / 'routing' / 'mihomo.exe'


@unittest.skipUnless(os.name == 'nt' and MIHOMO.is_file(),
                     '需要项目内置 Windows Mihomo 运行时')
class RoutingAutoPolicyMihomoTests(unittest.TestCase):
    def test_simple_dns_domestic_doh_fallback_passes_mihomo_config_check(self):
        with tempfile.TemporaryDirectory() as root:
            config = routing.normalize_config({
                **routing.default_config(),
                'dns_mode': 'simple',
                'physical_interface': '以太网',
            })
            generated = routing.build_mihomo_config(config, [])
            config_path = Path(root) / 'config.json'
            with config_path.open('w', encoding='utf-8') as stream:
                json.dump(generated, stream, ensure_ascii=False, indent=2)

            routing.RoutingManager()._test_config(str(config_path), root)

        self.assertEqual(generated['dns']['fallback'], [
            'https://dns.alidns.com/dns-query',
            'https://doh.pub/dns-query',
        ])

    def test_failure_and_percent_groups_pass_mihomo_config_check(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
                os.environ, {'LOCALAPPDATA': root}):
            config = routing.normalize_config({
                **routing.default_config(),
                'default_outbound': 'proxy:alpha',
                'proxy_providers': [{
                    'id': 'alpha',
                    'name': '验证订阅',
                    'url': 'https://example.test/subscription',
                    'enabled': True,
                    'strategy': 'url-test',
                    'selection_mode': 'auto',
                    'auto_policy': {
                        'enabled': True,
                        'stages': [{
                            'region': 'JP',
                            'selection_mode': 'failure',
                            'preferred_node': '🇯🇵 Tōkyo 日本东京 08｜高速专线',
                            'preferred_keywords': ['高速专线'],
                        }, {
                            'region': 'US',
                            'selection_mode': 'latency',
                            'preferred_keywords': ['高速专线'],
                        }],
                        'fallback': 'all',
                        'latency_tolerance': 20,
                        'latency_tolerance_unit': 'percent',
                    },
                }],
            })
            provider = config['proxy_providers'][0]
            nodes = [{
                'name': '🇯🇵 Tōkyo 日本东京 08｜高速专线',
                'display_name': '🇯🇵 Tōkyo 日本东京 08｜高速专线',
                'type': 'socks5', 'delay': 86, 'alive': True,
                'tested': True, 'tested_at': 1,
            }, {
                'name': '日本东京 09',
                'display_name': '日本东京 09',
                'type': 'socks5', 'delay': 92, 'alive': True,
                'tested': True, 'tested_at': 1,
            }, {
                'name': '美国洛杉矶 01｜高速专线',
                'display_name': '美国洛杉矶 01｜高速专线',
                'type': 'socks5', 'delay': 120, 'alive': True,
                'tested': True, 'tested_at': 1,
            }]
            subscription_store.persist_node_snapshot(provider, nodes)
            provider_dir = Path(root) / 'providers'
            provider_dir.mkdir()
            proxy_items = [{
                'name': item['name'], 'type': 'socks5',
                'server': '127.0.0.1', 'port': 1080,
            } for item in nodes]
            with (provider_dir / subscription_store.provider_filename(
                    provider)).open('w', encoding='utf-8') as stream:
                json.dump({'proxies': proxy_items}, stream,
                          ensure_ascii=False, indent=2)
            config_path = Path(root) / 'config.json'
            with config_path.open('w', encoding='utf-8') as stream:
                json.dump(routing.build_mihomo_config(config, []), stream,
                          ensure_ascii=False, indent=2)

            routing.RoutingManager()._test_config(str(config_path), root)


if __name__ == '__main__':
    unittest.main()
