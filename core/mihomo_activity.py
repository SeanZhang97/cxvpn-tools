# -*- coding: utf-8 -*-
"""Mihomo 连接与核心日志中继；Controller 凭据只留在 Python 后端。"""
import copy
import json
import os
import re
import socket
import threading
import time

from .mihomo_telemetry import _FrameReader, _client_frame, _open_websocket


MAX_ACTIVE_CONNECTIONS = 1000
MAX_CLOSED_CONNECTIONS = 500
MAX_CORE_LOGS = 1000
MAX_TEXT = 2048
RETRY_DELAYS = (1, 2, 4, 8, 15)
VALID_VIEWS = {'idle', 'connections', 'logs'}
_SENSITIVE_QUERY = re.compile(
    r'(?i)([?&](?:token|secret|password|passwd|key|auth)=)[^&\s]+')
_URL_CREDENTIALS = re.compile(r'(?i)([a-z][a-z0-9+.-]*://)[^/@\s]+@')
_BEARER_TOKEN = re.compile(r'(?i)(\bBearer\s+)[A-Za-z0-9._~+\-/=]+')


def _text(value, limit=MAX_TEXT):
    return str(value or '')[:limit]


def _safe_int(value):
    try:
        return min(max(0, int(value or 0)), (1 << 53) - 1)
    except (TypeError, ValueError, OverflowError):
        return 0


def _safe_process(value):
    """进程路径只保留文件名，避免日志暴露当前用户目录。"""
    text = _text(value, 512).replace('/', '\\')
    return os.path.basename(text) if text else ''


def _sanitize_connection(value):
    if not isinstance(value, dict):
        return None
    metadata = value.get('metadata')
    metadata = metadata if isinstance(metadata, dict) else {}
    connection_id = _text(value.get('id'), 160)
    if not connection_id:
        return None
    chains = value.get('chains')
    if not isinstance(chains, list):
        chains = []
    return {
        'id': connection_id,
        'metadata': {
            'network': _text(metadata.get('network'), 24),
            'type': _text(metadata.get('type'), 32),
            'host': _text(metadata.get('host'), 512),
            'destination_ip': _text(metadata.get('destinationIP'), 96),
            'destination_port': _text(metadata.get('destinationPort'), 16),
            'process': _safe_process(metadata.get('process')),
            'process_path': _safe_process(metadata.get('processPath')),
        },
        'upload': _safe_int(value.get('upload')),
        'download': _safe_int(value.get('download')),
        'start': _text(value.get('start'), 64),
        'chains': [_text(item, 256) for item in chains[:16]],
        'rule': _text(value.get('rule'), 128),
        'rule_payload': _text(value.get('rulePayload'), 512),
    }


def _sanitize_log_payload(value, secrets=()):
    text = _text(value)
    for secret in secrets:
        secret = str(secret or '')
        if secret:
            text = text.replace(secret, '[redacted]')
    text = _URL_CREDENTIALS.sub(r'\1[redacted]@', text)
    text = _SENSITIVE_QUERY.sub(r'\1[redacted]', text)
    return _BEARER_TOKEN.sub(r'\1[redacted]', text)


class MihomoActivityRelay:
    """按当前页面订阅 `/connections` 或 `/logs`，并发布有界快照。"""

    def __init__(self, config_getter, routing_manager, log):
        self._config_getter = config_getter
        self._routing = routing_manager
        self._log = log
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._thread = None
        self._view = 'idle'
        self._lifecycle_version = 0
        self._versions = {'connections': 0, 'logs': 0}
        self._states = {
            'connections': {
                'phase': 'idle', 'retry_seconds': 0,
                'upload_total': 0, 'download_total': 0,
                'active': [], 'closed': [],
            },
            'logs': {
                'phase': 'idle', 'retry_seconds': 0, 'items': [],
            },
        }

    def start(self):
        with self._condition:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name='mihomo-activity', daemon=True)
        self._log('[routing-activity] 后端连接与日志任务已提交，等待页面订阅')
        self._thread.start()

    def stop(self):
        self._log('[routing-activity] 已提交停止请求')
        self._stop.set()
        with self._condition:
            self._lifecycle_version += 1
            self._condition.notify_all()
            thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=3)

    def set_view(self, view):
        view = str(view or 'idle').strip().lower()
        if view not in VALID_VIEWS:
            return False
        with self._condition:
            if self._view == view:
                return True
            previous = self._view
            self._view = view
            self._lifecycle_version += 1
            if previous in self._versions:
                self._set_phase_locked(previous, 'idle', 0)
            if view in self._versions:
                self._versions[view] += 1
            self._condition.notify_all()
        self._log(
            f'[routing-activity] UI 已切换到'
            f'{"连接" if view == "connections" else "核心日志" if view == "logs" else "空闲"}订阅')
        return True

    def snapshot(self, view):
        view = str(view or '').strip().lower()
        with self._condition:
            if view not in self._versions:
                return {'ok': False, 'version': 0, 'snapshot': None}
            return {
                'ok': True,
                'version': self._versions[view],
                'snapshot': copy.deepcopy(self._states[view]),
            }

    def wait(self, after_version=0, view='connections', timeout=25):
        view = str(view or '').strip().lower()
        if view not in self._versions:
            return {'ok': False, 'version': 0, 'changed': False,
                    'snapshot': None}
        try:
            cursor = max(0, int(after_version or 0))
        except (TypeError, ValueError):
            cursor = 0
        try:
            wait_seconds = min(30, max(1, float(timeout or 25)))
        except (TypeError, ValueError):
            wait_seconds = 25
        with self._condition:
            deadline = time.monotonic() + wait_seconds
            while (not self._stop.is_set() and
                   self._versions[view] <= cursor):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            changed = self._versions[view] > cursor
            return {
                'ok': not self._stop.is_set(),
                'version': self._versions[view],
                'changed': changed,
                'resync': cursor > self._versions[view],
                'snapshot': (copy.deepcopy(self._states[view])
                             if changed else None),
            }

    def clear(self, view):
        view = str(view or '').strip().lower()
        with self._condition:
            if view == 'connections':
                self._states['connections']['closed'] = []
            elif view == 'logs':
                self._states['logs']['items'] = []
            else:
                return False
            self._versions[view] += 1
            self._condition.notify_all()
        return True

    def close_connection(self, connection_id=''):
        config = self._config_getter().get('routing') or {}
        return self._routing.close_connections(
            config, _text(connection_id, 160))

    def _set_phase_locked(self, view, phase, retry_seconds=0):
        state = self._states[view]
        retry = min(15, max(0, int(retry_seconds or 0)))
        if state['phase'] == phase and state['retry_seconds'] == retry:
            return False
        state['phase'] = phase
        state['retry_seconds'] = retry
        self._versions[view] += 1
        return True

    def _set_phase(self, view, phase, retry_seconds=0):
        with self._condition:
            changed = self._set_phase_locked(view, phase, retry_seconds)
            if changed:
                self._condition.notify_all()
        if changed:
            detail = f'，{retry_seconds} 秒后重试' if retry_seconds else ''
            self._log(f'[routing-activity] {view} 通道状态={phase}{detail}')

    def _active_snapshot(self):
        with self._condition:
            return self._view, self._lifecycle_version

    def _wait_lifecycle(self, timeout, version):
        with self._condition:
            if (self._lifecycle_version == version and
                    not self._stop.is_set()):
                self._condition.wait(timeout)

    def _apply_connections(self, payload):
        rows = payload.get('connections') if isinstance(payload, dict) else []
        rows = rows if isinstance(rows, list) else []
        active = []
        for row in rows[:MAX_ACTIVE_CONNECTIONS]:
            safe = _sanitize_connection(row)
            if safe:
                active.append(safe)
        with self._condition:
            state = self._states['connections']
            previous = {item['id']: item for item in state['active']}
            active_ids = {item['id'] for item in active}
            removed = [item for key, item in previous.items()
                       if key not in active_ids]
            if removed:
                state['closed'] = (
                    state['closed'] + removed)[-MAX_CLOSED_CONNECTIONS:]
            state['active'] = active
            state['upload_total'] = _safe_int(payload.get('uploadTotal'))
            state['download_total'] = _safe_int(payload.get('downloadTotal'))
            self._versions['connections'] += 1
            self._condition.notify_all()

    def _log_secrets(self, config):
        values = [config.get('controller_secret')]
        for provider in config.get('proxy_providers') or []:
            if isinstance(provider, dict):
                values.extend((provider.get('url'), provider.get('download_proxy')))
        return tuple(item for item in values if item)

    def _apply_log(self, payload, config):
        if not isinstance(payload, dict):
            return
        level = str(payload.get('type') or 'info').strip().lower()
        if level == 'warn':
            level = 'warning'
        if level not in {'debug', 'info', 'warning', 'error'}:
            level = 'info'
        item = {
            'time': time.strftime('%H:%M:%S'),
            'type': level,
            'payload': _sanitize_log_payload(
                payload.get('payload'), self._log_secrets(config)),
        }
        with self._condition:
            state = self._states['logs']
            state['items'] = (state['items'] + [item])[-MAX_CORE_LOGS:]
            self._versions['logs'] += 1
            self._condition.notify_all()

    def _stream(self, view, config, lifecycle_version):
        path = '/connections' if view == 'connections' else '/logs?level=debug'
        connection, initial = _open_websocket(
            config, timeout=3, path=path)
        self._set_phase(view, 'connected')
        reader = _FrameReader(connection, initial)
        fragments = bytearray()
        fragment_opcode = None
        try:
            while not self._stop.is_set():
                current_view, current_version = self._active_snapshot()
                if current_view != view or current_version != lifecycle_version:
                    return
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
                if view == 'connections':
                    self._apply_connections(value)
                else:
                    self._apply_log(value, config)
        finally:
            try:
                connection.sendall(_client_frame(0x8))
            except OSError:
                pass
            connection.close()

    def _run(self):
        self._log('[routing-activity] 后台已领取任务，等待页面激活')
        retry_index = 0
        try:
            while not self._stop.is_set():
                with self._condition:
                    while self._view == 'idle' and not self._stop.is_set():
                        self._condition.wait(1)
                if self._stop.is_set():
                    break
                view, lifecycle_version = self._active_snapshot()
                config = self._config_getter().get('routing') or {}
                try:
                    status = self._routing.status(config, quick=True)
                    if not (status.get('running') or status.get('core_running')):
                        retry_index = 0
                        self._set_phase(view, 'idle')
                        self._wait_lifecycle(1, lifecycle_version)
                        continue
                    self._set_phase(view, 'connecting')
                    self._stream(view, config, lifecycle_version)
                    retry_index = 0
                    self._wait_lifecycle(0.25, lifecycle_version)
                except Exception as exc:
                    current_view, current_version = self._active_snapshot()
                    if current_view != view or current_version != lifecycle_version:
                        continue
                    delay = RETRY_DELAYS[min(
                        retry_index, len(RETRY_DELAYS) - 1)]
                    retry_index += 1
                    self._log(
                        f'[routing-activity] {view} 通道失败: '
                        f'{type(exc).__name__}，{delay} 秒后重试')
                    self._set_phase(view, 'reconnecting', delay)
                    self._wait_lifecycle(delay, lifecycle_version)
        finally:
            with self._condition:
                for view in self._versions:
                    self._set_phase_locked(view, 'idle', 0)
                self._condition.notify_all()
            self._log('[routing-activity] 后端任务已停止')
