# -*- coding: utf-8 -*-
"""仅运行内置核心 -t；本地 file provider，不启动代理或访问订阅。"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import routing
from tests.test_routing_business import sample, NODE


class BusinessMihomoTests(unittest.TestCase):
    def test_local_groups_unicode_exclusions_and_missing_node_parse(self):
        with tempfile.TemporaryDirectory(prefix='cxvpn-business-core-') as root, \
                patch.dict(os.environ, {'LOCALAPPDATA': root}):
            config = sample()
            generated = routing.build_mihomo_config(config, [])
            provider_path = Path(root, 'local-provider.json')
            with provider_path.open('w', encoding='utf-8') as stream:
                json.dump({'proxies': [{'name': node, 'type': 'socks5', 'server': '127.0.0.1', 'port': 1}
                                      for node in [NODE, '节点 B']]}, stream, ensure_ascii=False)
            generated['proxy-providers']['provider-alpha'] = {
                'type': 'file', 'path': str(provider_path),
                'override': {'additional-prefix': '[订阅甲] '},
                'health-check': {'enable': False},
            }
            # 下载辅助组引用的临时 provider 也只能读取本地文件。
            for key in generated['proxy-providers']:
                generated['proxy-providers'][key] = dict(generated['proxy-providers']['provider-alpha'])
            generated['proxy-groups'].append({
                'name': 'EMPTY-TEST', 'type': 'select', 'use': ['provider-alpha'],
                'filter': '^missing$', 'empty-fallback': 'REJECT'})
            path = Path(root, 'config.json')
            with path.open('w', encoding='utf-8') as stream:
                json.dump(generated, stream, ensure_ascii=False)
            routing.RoutingManager()._test_config(str(path), root)


if __name__ == '__main__':
    unittest.main()
