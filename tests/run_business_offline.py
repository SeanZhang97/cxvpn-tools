# -*- coding: utf-8 -*-
"""业务分流相关离线验证，隔离用户数据并禁止真实网络、服务和子进程。"""
import os
import tempfile
import unittest
from unittest.mock import patch


def main():
    with tempfile.TemporaryDirectory(prefix='cxvpn-business-suite-') as root, \
            patch.dict(os.environ, {'LOCALAPPDATA': root}):
        from core import config
        names = ['routing_business', 'routing_schema', 'routing_aggregate', 'routing_selection',
                 'routing_commit', 'config_maintenance', 'routing', 'routing_optimization',
                 'routing_readiness', 'routing_reliability', 'routing_lifecycle', 'routing_service']
        suite = unittest.defaultTestLoader.loadTestsFromNames(['tests.test_' + name for name in names])
        with patch.object(config, 'BASE', root), \
                patch('subprocess.Popen', side_effect=AssertionError('离线测试缺少进程模拟')), \
                patch('socket.socket.connect', side_effect=AssertionError('离线测试缺少网络模拟')), \
                patch('core.routing_service._open_pipe', side_effect=AssertionError('离线测试缺少服务模拟')):
            result = unittest.TextTestRunner(verbosity=1).run(suite)
        return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
