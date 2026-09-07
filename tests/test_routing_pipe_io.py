# -*- coding: utf-8 -*-
"""仅连接随机测试管道，验证 Windows 重叠 I/O 与 UTF-8 分帧。"""
import ctypes
from ctypes import wintypes
import json
import struct
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from core import routing_service


class PipeIoTests(unittest.TestCase):
    def exchange(self, payload, stall=False):
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR] + [wintypes.DWORD] * 6 + [ctypes.c_void_p]
        kernel.CreateNamedPipeW.restype = wintypes.HANDLE
        kernel.ConnectNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
        for operation in (kernel.ReadFile, kernel.WriteFile):
            operation.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                  ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        name = r'\\.\pipe\cxvpn-test-' + uuid.uuid4().hex
        pipe = kernel.CreateNamedPipeW(name, 3, 0, 1, 65536, 65536, 0, None)
        self.assertNotEqual(pipe, ctypes.c_void_p(-1).value)
        release, received = threading.Event(), []
        failures = []

        def read(size):
            buffer = ctypes.create_string_buffer(size)
            count = wintypes.DWORD()
            if not kernel.ReadFile(pipe, buffer, size, ctypes.byref(count), None):
                raise OSError('test pipe read')
            return buffer.raw[:count.value]

        def server():
            try:
                if not kernel.ConnectNamedPipe(pipe, None) and ctypes.get_last_error() != 535:
                    raise OSError('test pipe connect')
                size = struct.unpack('<I', read(4))[0]
                received.append(json.loads(read(size).decode('utf-8')))
                if stall:
                    release.wait(2)
                else:
                    body = json.dumps({'ok': True, 'data': payload}, ensure_ascii=False).encode('utf-8')
                    data = struct.pack('<I', len(body)) + body
                    count = wintypes.DWORD()
                    if not kernel.WriteFile(pipe, data, len(data), ctypes.byref(count), None):
                        raise OSError('test pipe write')
                    release.wait(2)
            except Exception as exc:
                failures.append(exc)
            finally:
                kernel.DisconnectNamedPipe(pipe)
                kernel.CloseHandle(pipe)

        worker = threading.Thread(target=server, daemon=True)
        worker.start()
        try:
            with patch.object(routing_service, 'PIPE_NAME', name):
                client = routing_service.RoutingServiceClient('unused-test.exe')
                if stall:
                    started = time.monotonic()
                    with self.assertRaisesRegex(routing_service.ServiceError, '超时'):
                        client.request(payload, response_timeout=.15)
                    self.assertLess(time.monotonic() - started, 1)
                else:
                    self.assertEqual(client.request(payload, response_timeout=2), payload)
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertFalse(failures)
        self.assertEqual(received, [payload])

    def test_utf8_json_round_trip(self):
        self.exchange({'name': '订阅 e\u0301 🇨🇳', 'nodes': ['东京 🇯🇵', '香港']})

    def test_connected_server_that_never_replies_is_cancelled(self):
        self.exchange({'op': 'test'}, stall=True)


if __name__ == '__main__':
    unittest.main()
