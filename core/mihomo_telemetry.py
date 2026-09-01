# -*- coding: utf-8 -*-
"""后端 Mihomo 遥测中继：UI 不接触 Controller 地址与 Secret。"""
import base64
import hashlib
import json
import os
import socket
import struct
import threading
import time


RETRY_DELAYS = (1, 2, 4, 8, 15)
MAX_FRAME_SIZE = 1024 * 1024


def _safe_bytes(value):
    try:
        number = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return min(max(0, number), (1 << 53) - 1)


def _client_frame(opcode, payload=b''):
    """RFC 6455 客户端帧必须掩码；仅用于 ping/pong/close。"""
    payload = bytes(payload or b'')
    mask = os.urandom(4)
    length = len(payload)
    head = bytearray([0x80 | (opcode & 0x0F)])
    if length < 126:
        head.append(0x80 | length)
    elif length <= 0xFFFF:
        head.append(0x80 | 126)
        head.extend(struct.pack('!H', length))
    else:
        head.append(0x80 | 127)
        head.extend(struct.pack('!Q', length))
    head.extend(mask)
    head.extend(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return bytes(head)


class _FrameReader:
    def __init__(self, connection, initial=b''):
        self.connection = connection
        self.buffer = bytearray(initial)

    def _take(self, size):
        while len(self.buffer) < size:
            chunk = self.connection.recv(max(4096, size - len(self.buffer)))
            if not chunk:
                raise ConnectionError('WebSocket connection closed')
            self.buffer.extend(chunk)
        value = bytes(self.buffer[:size])
        del self.buffer[:size]
        return value

    def read(self):
        first, second = self._take(2)
        final = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack('!H', self._take(2))[0]
        elif length == 127:
            length = struct.unpack('!Q', self._take(8))[0]
        if length > MAX_FRAME_SIZE:
            raise ValueError('WebSocket frame too large')
        mask = self._take(4) if masked else None
        payload = self._take(length)
        if mask:
            payload = bytes(
                byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return final, opcode, payload


def _open_websocket(config, timeout=3, path='/traffic'):
    port = int(config.get('controller_port') or 0)
    secret = str(config.get('controller_secret') or '')
    if not (1024 <= port <= 65535 and secret):
        raise ValueError('Mihomo controller configuration is incomplete')
    path = str(path or '')
    if not path.startswith('/') or '\r' in path or '\n' in path:
        raise ValueError('Mihomo websocket path is invalid')
    connection = socket.create_connection(('127.0.0.1', port), timeout=timeout)
    connection.settimeout(1)
    key = base64.b64encode(os.urandom(16)).decode('ascii')
    request = (
        f'GET {path} HTTP/1.1\r\n'
        f'Host: 127.0.0.1:{port}\r\n'
        'Upgrade: websocket\r\n'
        'Connection: Upgrade\r\n'
        f'Sec-WebSocket-Key: {key}\r\n'
        'Sec-WebSocket-Version: 13\r\n'
        f'Authorization: Bearer {secret}\r\n\r\n')
    try:
        connection.sendall(request.encode('utf-8'))
        response = bytearray()
        while b'\r\n\r\n' not in response:
            chunk = connection.recv(4096)
            if not chunk:
                raise ConnectionError('WebSocket handshake closed')
            response.extend(chunk)
            if len(response) > 16384:
                raise ValueError('WebSocket handshake too large')
        header, initial = bytes(response).split(b'\r\n\r\n', 1)
        lines = header.decode('iso-8859-1').split('\r\n')
        if not lines or ' 101 ' not in f' {lines[0]} ':
            raise ConnectionError('WebSocket handshake rejected')
        headers = {}
        for line in lines[1:]:
            if ':' in line:
                name, value = line.split(':', 1)
                headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1(
            (key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode(
                'ascii')).digest()).decode('ascii')
        if headers.get('sec-websocket-accept') != expected:
            raise ConnectionError('WebSocket handshake validation failed')
        return connection, initial
    except Exception:
        connection.close()
        raise


class MihomoTelemetryRelay:
    """按 UI 可见性启停后端 WebSocket，并发布脱敏聚合数据。"""

    def __init__(self, config_getter, routing_manager, log, on_change=None):
        self._config_getter = config_getter
        self._routing = routing_manager
        self._log = log
        self._on_change = on_change or (lambda: None)
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._thread = None
        self._active = False
        self._lifecycle_version = 0
        self._state = {
            'phase': 'idle', 'retry_seconds': 0,
            'up': 0, 'down': 0, 'up_total': 0, 'down_total': 0,
            'observability': {
                'available': False, 'active': 0,
                'upload': 0, 'download': 0, 'rule_hits': [],
            },
        }

    def start(self):
        with self._condition:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name='mihomo-telemetry', daemon=True)
        self._log('[routing-stream] 后端遥测任务已提交，等待页面订阅')
        self._thread.start()

    def stop(self):
        self._log('[routing-stream] 已提交后端遥测停止请求')
        self._stop.set()
        with self._condition:
            self._lifecycle_version += 1
            self._condition.notify_all()
            thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=3)

    def set_active(self, active):
        desired = bool(active)
        with self._condition:
            if desired == self._active:
                return True
            self._active = desired
            self._lifecycle_version += 1
            self._condition.notify_all()
        self._log(
            f'[routing-stream] UI 已提交后端遥测'
            f'{"订阅" if desired else "暂停"}请求')
        if not desired:
            self._update(phase='idle', retry_seconds=0, reset_traffic=True)
        return True

    def snapshot(self):
        with self._condition:
            return json.loads(json.dumps(self._state))

    def _is_active(self):
        with self._condition:
            return self._active and not self._stop.is_set()

    def _lifecycle_snapshot(self):
        with self._condition:
            return self._lifecycle_version

    def _wait_lifecycle(self, timeout, version):
        """只等待同一页面生命周期；离开再返回会立即废弃旧退避。"""
        with self._condition:
            if (self._lifecycle_version == version and
                    not self._stop.is_set()):
                self._condition.wait(timeout)

    def _update(self, phase=None, retry_seconds=None, traffic=None,
                observability=None, reset_traffic=False):
        changed = False
        phase_changed = False
        with self._condition:
            if phase is not None and self._state['phase'] != phase:
                self._state['phase'] = phase
                phase_changed = True
                changed = True
            if retry_seconds is not None:
                retry = min(15, max(0, int(retry_seconds)))
                if self._state['retry_seconds'] != retry:
                    self._state['retry_seconds'] = retry
                    changed = True
            if reset_traffic:
                for key in ('up', 'down', 'up_total', 'down_total'):
                    if self._state[key]:
                        self._state[key] = 0
                        changed = True
            for key, value in (traffic or {}).items():
                if key in {'up', 'down', 'up_total', 'down_total'}:
                    safe = _safe_bytes(value)
                    if self._state[key] != safe:
                        self._state[key] = safe
                        changed = True
            if observability is not None and self._state['observability'] != observability:
                self._state['observability'] = json.loads(
                    json.dumps(observability))
                changed = True
        if phase_changed:
            detail = (f'，{self._state["retry_seconds"]} 秒后重试'
                      if phase == 'reconnecting' else '')
            self._log(f'[routing-stream] 后端实时流量通道状态={phase}{detail}')
        if changed:
            self._on_change()

    def _refresh_observability(self, config):
        try:
            value = self._routing.connection_observability(config)
        except Exception as exc:
            self._log(
                f'[routing-stream] 后端脱敏摘要读取失败: {type(exc).__name__}')
            value = {
                'available': False, 'active': 0, 'upload': 0,
                'download': 0, 'rule_hits': [],
            }
        self._update(observability=value)

    def _stream(self, config):
        started_at = time.monotonic()
        self._log('[routing-stream] 开始连接 Mihomo 聚合流量端点，超时 3 秒')
        connection, initial = _open_websocket(config, timeout=3)
        self._log(
            f'[routing-stream] Mihomo 聚合流量端点连接成功，耗时 '
            f'{time.monotonic() - started_at:.3f} 秒')
        self._update(phase='connected', retry_seconds=0)
        reader = _FrameReader(connection, initial)
        fragments = bytearray()
        fragment_opcode = None
        next_observability = 0
        try:
            while self._is_active():
                now = time.monotonic()
                if now >= next_observability:
                    self._refresh_observability(config)
                    next_observability = now + 10
                try:
                    final, opcode, payload = reader.read()
                except socket.timeout:
                    continue
                if opcode == 0x8:
                    return
                if opcode == 0x9:
                    connection.sendall(_client_frame(0xA, payload))
                    continue
                if opcode == 0xA:
                    continue
                if opcode == 0x1:
                    fragments = bytearray(payload)
                    fragment_opcode = opcode
                elif opcode == 0x0 and fragment_opcode == 0x1:
                    fragments.extend(payload)
                else:
                    continue
                if not final:
                    continue
                raw = bytes(fragments)
                fragments.clear()
                fragment_opcode = None
                try:
                    value = json.loads(raw.decode('utf-8'))
                except (UnicodeError, ValueError, TypeError):
                    continue
                self._update(traffic={
                    'up': value.get('up'), 'down': value.get('down'),
                    'up_total': value.get('upTotal'),
                    'down_total': value.get('downTotal'),
                })
        finally:
            try:
                connection.sendall(_client_frame(0x8))
            except OSError:
                pass
            connection.close()

    def _run(self):
        self._log('[routing-stream] 后台已领取遥测任务，等待页面激活')
        retry_index = 0
        try:
            while not self._stop.is_set():
                with self._condition:
                    while not self._active and not self._stop.is_set():
                        self._condition.wait(1)
                if self._stop.is_set():
                    break
                config = self._config_getter().get('routing') or {}
                lifecycle_version = self._lifecycle_snapshot()
                try:
                    status = self._routing.status(config, quick=True)
                    if not status.get('running'):
                        retry_index = 0
                        self._update(
                            phase='idle', retry_seconds=0,
                            reset_traffic=True)
                        self._wait_lifecycle(1, lifecycle_version)
                        continue
                    self._update(phase='connecting', retry_seconds=0)
                    self._stream(config)
                    retry_index = 0
                except Exception as exc:
                    if not self._is_active():
                        continue
                    delay = RETRY_DELAYS[min(
                        retry_index, len(RETRY_DELAYS) - 1)]
                    retry_index += 1
                    self._log(
                        f'[routing-stream] Mihomo 聚合流量连接失败: '
                        f'{type(exc).__name__}，{delay} 秒后重试')
                    self._update(
                        phase='reconnecting', retry_seconds=delay,
                        reset_traffic=True)
                    self._wait_lifecycle(delay, lifecycle_version)
        finally:
            self._update(phase='idle', retry_seconds=0, reset_traffic=True)
            self._log('[routing-stream] 后端遥测任务已停止')
