# -*- coding: utf-8 -*-
"""代理启停相关离线回归；拒绝未模拟的 Windows、服务、网络和进程调用。"""
from contextlib import ExitStack
import os
import tempfile
import threading
import unittest
from unittest.mock import patch


def main():
    modules = [
        'test_routing_lifecycle', 'test_invalid_proxy_repair', 'test_api_proxy_lifecycle',
        'test_proxycore', 'test_proxy_guard', 'test_routing_reliability',
        'test_routing_optimization', 'test_routing_schema', 'test_routing',
    ]
    # 不允许挂起的离线检查越过项目单项 60 秒边界。
    timeout = threading.Timer(60, lambda: os._exit(124))
    timeout.daemon = True
    timeout.start()
    try:
        with tempfile.TemporaryDirectory(prefix='cxvpn-offline-lifecycle-') as root, ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {key: root for key in (
                'LOCALAPPDATA', 'APPDATA', 'USERPROFILE', 'ProgramData', 'CODEX_HOME')}))
            suite = unittest.defaultTestLoader.loadTestsFromNames(['tests.' + name for name in modules])
            for name in ('subprocess.Popen', 'socket.socket.connect', 'core.routing_service._open_pipe',
                         'winreg.OpenKey', 'winreg.CreateKey', 'winreg.SetValueEx', 'ctypes.WinDLL'):
                stack.enter_context(patch(name, side_effect=AssertionError('offline boundary: ' + name)))
            result = unittest.TextTestRunner(verbosity=1).run(suite)
            return 0 if result.wasSuccessful() else 1
    finally:
        timeout.cancel()


if __name__ == '__main__':
    raise SystemExit(main())
