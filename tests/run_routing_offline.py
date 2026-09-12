# -*- coding: utf-8 -*-
"""本次代理改动的离线验证入口，拒绝未模拟的网络、服务和子进程访问。"""
import os
import tempfile
import unittest
from unittest.mock import patch


def main():
    with tempfile.TemporaryDirectory(prefix='cxvpn-offline-') as root, \
            patch.dict(os.environ, {'LOCALAPPDATA': root}):
        from core import config
        modules = [
            'test_codex_proxy',
            'test_routing', 'test_routing_optimization', 'test_routing_commit',
            'test_routing_service', 'test_routing_speedtest', 'test_routing_reliability',
            'test_routing_selection', 'test_routing_aggregate', 'test_routing_schema', 'test_routing_auto_policy',
            'test_api_routing_stream',
        ]
        suite = unittest.defaultTestLoader.loadTestsFromNames(
            ['tests.' + name for name in modules])
        with patch.object(config, 'BASE', root), \
                patch('subprocess.Popen', side_effect=AssertionError('离线测试缺少进程模拟')), \
                patch('socket.socket.connect', side_effect=AssertionError('离线测试缺少网络模拟')), \
                patch('core.routing_service._open_pipe', side_effect=AssertionError('离线测试缺少服务模拟')):
            result = unittest.TextTestRunner(verbosity=1).run(suite)
        return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
