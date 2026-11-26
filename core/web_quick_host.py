# -*- coding: utf-8 -*-
"""Edge 原生消息宿主：Edge 以 chrome-extension:// 参数启动本程序时，在标准流与主程序管道间转发一条消息。"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import sys
import time
import winreg

from core import web_quick_pipe
from core.pipe_io import pipe_stream

HOST_NAME = 'com.cxvpntools.web_quick'
REGISTRY_ROOT = 'SOFTWARE\\Microsoft\\Edge\\NativeMessagingHosts'
LOG_NAME = 'web-quick-host.log'
MAX_NATIVE_MESSAGE = 64 * 1024
APP_CONNECT_MS = 1500
LOG_LIMIT = 256 * 1024


def _log_line(text):
    """宿主没有主程序日志可写；只记录事件名与异常类型，保留在用户数据目录。"""
    try:
        local = os.environ.get('LOCALAPPDATA', '')
        if not local:
            return
        target = os.path.join(local, 'CXVPNTools', LOG_NAME)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.isfile(target) and os.path.getsize(target) > LOG_LIMIT:
            os.remove(target)
        with open(target, 'a', encoding='utf-8') as stream:
            stream.write(f'{time.strftime("%Y-%m-%d %H:%M:%S")} {text}\n')
    except OSError:
        pass


def _read_native(stream):
    header = stream.read(4)
    (length,) = struct.unpack('<I', header) if len(header) == 4 else (0,)
    if not 0 < length <= MAX_NATIVE_MESSAGE:
        raise ValueError('native message out of range')
    body = stream.read(length)
    if len(body) != length:
        raise ValueError('native message truncated')
    return json.loads(body.decode('utf-8'))


def _write_native(stream, value):
    body = json.dumps(value, ensure_ascii=False).encode('utf-8')
    stream.write(struct.pack('<I', len(body)) + body)
    stream.flush()


def relay(request):
    """转发到主程序管道；连接不上时返回固定错误码，不附加系统消息。"""
    try:
        return web_quick_pipe.request(web_quick_pipe.PIPE_NAME, request, APP_CONNECT_MS)
    except OSError as exc:
        return {'ok': False, 'error': 'app_unreachable' if exc.winerror == 5 else 'app_not_running'}


def run_child():
    try:
        request = _read_native(pipe_stream('stdin'))
        kind = request.get('type') if isinstance(request, dict) else 'invalid'
        _log_line(f'relay start type={kind}')
        started = time.monotonic()
        response = relay(request)
        _log_line(f'relay done result={response.get("error", "ok")} '
                  f'elapsed={time.monotonic() - started:.2f}s')
    except Exception as exc:
        _log_line(f'relay failed error={type(exc).__name__}')
        response = {'ok': False, 'error': 'host_error'}
    _write_native(pipe_stream('stdout'), response)


def extension_id(manifest_path):
    """扩展 ID 由 manifest 公钥确定：SHA-256 前 16 字节，每个半字节依次映射为 a-p。"""
    with open(manifest_path, encoding='utf-8') as stream:
        key = json.load(stream)['key']
    digest = hashlib.sha256(base64.b64decode(key)).digest()
    return ''.join('abcdefghijklmnop'[nibble] for byte in digest[:16]
                   for nibble in (byte >> 4, byte & 15))


def register(root, executable, extension_id, host_name=HOST_NAME):
    """登记宿主清单与当前用户注册表项；内容未变化时不重复写入。"""
    manifest = {
        'name': host_name,
        'description': 'CX Toolkit 网页快开自动登录宿主',
        'path': executable,
        'type': 'stdio',
        'allowed_origins': [f'chrome-extension://{extension_id}/'],
    }
    content = json.dumps(manifest, ensure_ascii=False, indent=2)
    target = os.path.join(root, host_name + '.json')
    try:
        current = ''
        if os.path.isfile(target):
            with open(target, encoding='utf-8') as stream:
                current = stream.read()
        if current != content:
            temporary = target + '.tmp'
            with open(temporary, 'w', encoding='utf-8') as stream:
                stream.write(content)
            os.replace(temporary, target)
        registry_path = 'SOFTWARE\\Microsoft\\Edge\\NativeMessagingHosts\\' + host_name
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, registry_path) as key:
            try:
                registered = str(winreg.QueryValueEx(key, None)[0])
            except FileNotFoundError:
                registered = ''
            if registered != target:
                winreg.SetValueEx(key, None, 0, winreg.REG_SZ, target)
    except (OSError, ValueError, KeyError) as exc:
        return {'ok': False, 'error': type(exc).__name__}
    return {'ok': True, 'path': target}
