# -*- coding: utf-8 -*-
import base64
import hashlib
import json
import socket
import threading
import time
import unittest
from unittest import mock

from core import mihomo_telemetry
from core.mihomo_telemetry import (
    MihomoTelemetryRelay, _FrameReader, _client_frame, _safe_bytes,
)


class FakeSocket:
    def __init__(self, payload):
        self.payload = bytearray(payload)

    def recv(self, size):
        if not self.payload:
            return b''
        value = bytes(self.payload[:size])
        del self.payload[:size]
        return value


def server_frame(opcode, payload=b''):
    payload = bytes(payload)
    if len(payload) < 126:
        return bytes([0x80 | opcode, len(payload)]) + payload
    return (bytes([0x80 | opcode, 126]) +
            len(payload).to_bytes(2, 'big') + payload)


def wait_until(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class LocalWebSocketServer:
    """只绑定临时回环端口，模拟 Mihomo 首次断线后恢复。"""

    def __init__(self):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(2)
        self.listener.settimeout(5)
        self.port = self.listener.getsockname()[1]
        self.requests = []
        self.pong = None
        self.error = None
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self._stop.set()
        self.listener.close()
        self.thread.join(timeout=2)

    @staticmethod
    def _request(connection):
        data = bytearray()
        while b'\r\n\r\n' not in data:
            chunk = connection.recv(4096)
            if not chunk:
                raise ConnectionError('client closed during handshake')
            data.extend(chunk)
        return bytes(data).split(b'\r\n\r\n', 1)[0].decode('iso-8859-1')

    @staticmethod
    def _accept_value(request):
        key = next(
            line.split(':', 1)[1].strip()
            for line in request.split('\r\n')
            if line.lower().startswith('sec-websocket-key:'))
        return base64.b64encode(hashlib.sha1(
            (key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode(
                'ascii')).digest()).decode('ascii')

    def _run(self):
        try:
            for index in range(2):
                connection, _address = self.listener.accept()
                connection.settimeout(3)
                with connection:
                    request = self._request(connection)
                    self.requests.append(request)
                    response = (
                        'HTTP/1.1 101 Switching Protocols\r\n'
                        'Upgrade: websocket\r\n'
                        'Connection: Upgrade\r\n'
                        f'Sec-WebSocket-Accept: {self._accept_value(request)}\r\n'
                        '\r\n')
                    connection.sendall(response.encode('ascii'))
                    if index == 0:
                        continue
                    payload = json.dumps({
                        'up': 1234, 'down': 5678,
                        'upTotal': 9012, 'downTotal': 34567,
                        'label': '中文 e\u0301 🇯🇵',
                    }, ensure_ascii=False).encode('utf-8')
                    connection.sendall(
                        server_frame(0x1, payload) +
                        server_frame(0x9, b'health'))
                    final, opcode, pong = _FrameReader(connection).read()
                    self.pong = (final, opcode, pong)
                    self._stop.wait(1)
        except (OSError, ConnectionError) as exc:
            if not self._stop.is_set():
                self.error = exc
        finally:
            try:
                self.listener.close()
            except OSError:
                pass


class MihomoTelemetryTests(unittest.TestCase):
    def test_client_control_frames_are_masked_and_round_trip(self):
        encoded = _client_frame(0xA, '中文🇯🇵'.encode('utf-8'))
        self.assertTrue(encoded[1] & 0x80)
        final, opcode, payload = _FrameReader(
            FakeSocket(encoded)).read()
        self.assertTrue(final)
        self.assertEqual(opcode, 0xA)
        self.assertEqual(payload.decode('utf-8'), '中文🇯🇵')

    def test_frame_reader_supports_extended_utf8_payload(self):
        payload = ('组合 e\u0301 与国旗 🇯🇵' * 20).encode('utf-8')
        frame = bytes([0x81, 126]) + len(payload).to_bytes(2, 'big') + payload
        final, opcode, decoded = _FrameReader(FakeSocket(frame)).read()
        self.assertTrue(final)
        self.assertEqual(opcode, 1)
        self.assertEqual(decoded, payload)

    def test_byte_values_are_sanitized_for_javascript(self):
        self.assertEqual(_safe_bytes(-1), 0)
        self.assertEqual(_safe_bytes('1024'), 1024)
        self.assertEqual(_safe_bytes(10 ** 30), (1 << 53) - 1)

    def test_backend_websocket_handshake_reconnect_and_utf8_traffic(self):
        server = LocalWebSocketServer()
        server.start()
        secret = 'local-integration-secret'
        logs = []
        routing = mock.Mock()
        routing.status.return_value = {'running': True}
        routing.connection_observability.return_value = {
            'available': True, 'active': 2,
            'upload': 10, 'download': 20,
            'rule_hits': [{'source': 'user', 'rule': 'DOMAIN', 'connections': 2}],
        }
        changed = threading.Event()
        relay = MihomoTelemetryRelay(
            lambda: {'routing': {
                'controller_port': server.port,
                'controller_secret': secret,
            }}, routing, logs.append, changed.set)
        relay.start()
        relay.set_active(True)
        try:
            self.assertTrue(wait_until(lambda: len(server.requests) == 2, 4))
            self.assertTrue(wait_until(
                lambda: relay.snapshot()['up'] == 1234, 3))
            self.assertTrue(wait_until(lambda: server.pong is not None, 2))
            snapshot = relay.snapshot()
            self.assertEqual(snapshot['phase'], 'connected')
            self.assertEqual(snapshot['down'], 5678)
            self.assertEqual(snapshot['up_total'], 9012)
            self.assertEqual(snapshot['down_total'], 34567)
            self.assertEqual(snapshot['observability']['active'], 2)
            self.assertEqual(server.pong, (True, 0xA, b'health'))
            for request in server.requests:
                self.assertTrue(request.startswith('GET /traffic HTTP/1.1'))
                self.assertIn(
                    f'Authorization: Bearer {secret}', request)
                self.assertNotIn('token=', request)
            self.assertNotIn(secret, '\n'.join(logs))
            relay.set_active(False)
            self.assertTrue(wait_until(
                lambda: relay.snapshot()['phase'] == 'idle', 0.5))
            self.assertEqual(relay.snapshot()['up'], 0)
        finally:
            relay.stop()
            server.stop()
        self.assertIsNone(server.error)

    def test_page_reentry_interrupts_obsolete_reconnect_delay(self):
        routing = mock.Mock()
        routing.status.return_value = {'running': True}
        attempts = []

        def fail_connect(_config, timeout=3):
            attempts.append((time.monotonic(), timeout))
            raise ConnectionError('expected local failure')

        relay = MihomoTelemetryRelay(
            lambda: {'routing': {
                'controller_port': 19090,
                'controller_secret': 'not-logged',
            }}, routing, mock.Mock())
        with mock.patch.object(
                mihomo_telemetry, '_open_websocket', side_effect=fail_connect), \
                mock.patch.object(
                    mihomo_telemetry, 'RETRY_DELAYS', (5,)):
            relay.start()
            relay.set_active(True)
            try:
                self.assertTrue(wait_until(
                    lambda: relay.snapshot()['phase'] == 'reconnecting', 1))
                relay.set_active(False)
                relay.set_active(True)
                self.assertTrue(wait_until(lambda: len(attempts) >= 2, 0.8))
            finally:
                relay.stop()


if __name__ == '__main__':
    unittest.main()
