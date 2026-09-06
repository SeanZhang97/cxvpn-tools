# -*- coding: utf-8 -*-
"""CXVPN 原生路由服务的安装、Named Pipe IPC 与配置事务客户端。"""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
import os
import struct
import subprocess
import tempfile
import time

from core.routing_support import binary_path, sha256_file


SERVICE_BINARY = 'CXVPNRoutingHost.exe'
SERVICE_VERSION = '0.6.1'
PROTOCOL_VERSION = 5
PIPE_NAME = r'\\.\pipe\CXVPNRoutingService.v1'
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class ServiceError(RuntimeError):
    pass


class ServiceUnavailable(ServiceError):
    pass


class RoutingServiceClient:
    def __init__(self, binary=None):
        self.binary = binary or binary_path(SERVICE_BINARY)

    def status(self):
        try:
            return self.request({'op': 'status'}, connect_timeout_ms=600)
        except ServiceUnavailable:
            return None

    def diagnostics(self):
        return self.request({'op': 'diagnostics'}, connect_timeout_ms=1500)

    def ensure_installed(self):
        status = self.status()
        if self._compatible(status):
            return status
        if not os.path.isfile(self.binary):
            raise ServiceError(f'统一分流运行时缺失：{SERVICE_BINARY}')
        owner_sid = current_user_sid()
        _run_elevated_binary(
            self.binary, ['--install', '--owner-sid', owner_sid], timeout=120)
        deadline = time.monotonic() + 12
        latest = None
        while time.monotonic() < deadline:
            latest = self.status()
            if self._compatible(latest):
                return latest
            time.sleep(0.25)
        if latest:
            raise ServiceError('新路由服务版本与客户端不兼容，请重新安装最新版')
        raise ServiceError('路由服务安装完成，但 IPC 未在限定时间内就绪')

    def apply(self, config_path, providers, runtime_mode='active',
              fast_toggle_ready=False, system_proxy_bypass_domains=None):
        runtime_mode = str(runtime_mode or '').strip().lower()
        if runtime_mode not in {'active', 'standby'}:
            raise ServiceError('路由服务运行模式无效')
        with open(config_path, 'rb') as stream:
            config = stream.read()
        rows = []
        for provider in providers:
            path = provider['path']
            with open(path, 'rb') as stream:
                content = stream.read()
            rows.append({
                'name': provider['name'],
                'content_b64': base64.b64encode(content).decode('ascii'),
                'sha256': sha256_file(path),
                'modified_at': max(0, int(os.path.getmtime(path))),
            })
        return self.request({
            'op': 'apply',
            'config_b64': base64.b64encode(config).decode('ascii'),
            'config_sha256': sha256_file(config_path),
            'providers': rows,
            'runtime_mode': runtime_mode,
            'fast_toggle_ready': bool(fast_toggle_ready),
            'system_proxy_bypass_domains': [
                str(item) for item in (system_proxy_bypass_domains or [])],
        }, connect_timeout_ms=3000)

    def read_provider(self, provider_name):
        result = self.request({
            'op': 'read_provider',
            'provider_name': str(provider_name or ''),
        })
        encoded = str(result.get('content_b64') or '')
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise ServiceError('路由服务返回的 provider 缓存编码无效') from exc
        if not content:
            raise ServiceError('路由服务返回的 provider 缓存为空')
        return {
            'content': content,
            'modified_at': max(0, int(result.get('modified_at') or 0)),
        }

    def commit(self, transaction_id):
        return self.request({
            'op': 'commit', 'transaction_id': str(transaction_id or '')})

    def rollback(self, transaction_id):
        return self.request({
            'op': 'rollback', 'transaction_id': str(transaction_id or '')})

    def activate_system_proxy(self, transaction_id):
        return self.request({
            'op': 'activate_system_proxy',
            'transaction_id': str(transaction_id or ''),
        })

    def set_system_proxy_enabled(self, enabled):
        return self.request({
            'op': 'set_system_proxy_enabled',
            'enabled': bool(enabled),
        })

    def stop_runtime(self):
        return self.request({'op': 'stop_runtime'})

    def start_runtime(self):
        return self.request({'op': 'start_runtime'})

    def request(self, payload, connect_timeout_ms=2500):
        body = json.dumps(
            payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        handle = _open_pipe(connect_timeout_ms)
        try:
            _write_all(handle, struct.pack('<I', len(body)) + body)
            header = _read_exact(handle, 4)
            length = struct.unpack('<I', header)[0]
            if length <= 0 or length > MAX_RESPONSE_BYTES:
                raise ServiceError('路由服务返回了异常大小的响应')
            raw = _read_exact(handle, length)
        finally:
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle(handle)
        try:
            response = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ServiceError('路由服务响应格式异常') from exc
        if not isinstance(response, dict):
            raise ServiceError('路由服务响应格式异常')
        if not response.get('ok'):
            raise ServiceError(str(response.get('message') or '路由服务操作失败'))
        data = response.get('data')
        return data if isinstance(data, dict) else {}

    def _compatible(self, status):
        if not (status and
                status.get('protocol_version') == PROTOCOL_VERSION and
                status.get('service_version') == SERVICE_VERSION and
                os.path.isfile(self.binary)):
            return False
        return str(status.get('service_sha256') or '').upper() == \
            sha256_file(self.binary).upper()


def current_user_sid():
    advapi32 = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD)]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    class SidAndAttributes(ctypes.Structure):
        _fields_ = [('Sid', ctypes.c_void_p), ('Attributes', wintypes.DWORD)]

    class TokenUser(ctypes.Structure):
        _fields_ = [('User', SidAndAttributes)]

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise ServiceError('无法读取当前 Windows 用户身份')
    try:
        needed = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        if not needed.value:
            raise ServiceError('无法读取当前 Windows 用户 SID')
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(
                token, 1, buffer, needed, ctypes.byref(needed)):
            raise ServiceError('无法读取当前 Windows 用户 SID')
        sid_pointer = ctypes.cast(
            buffer, ctypes.POINTER(TokenUser)).contents.User.Sid
        sid_text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(
                sid_pointer, ctypes.byref(sid_text)):
            raise ServiceError('无法转换当前 Windows 用户 SID')
        try:
            value = str(sid_text.value or '')
        finally:
            kernel32.LocalFree(sid_text)
        if not value.startswith('S-1-'):
            raise ServiceError('当前 Windows 用户 SID 无效')
        return value
    finally:
        kernel32.CloseHandle(token)


def _run_elevated_binary(path, arguments, timeout=120):
    result = tempfile.NamedTemporaryFile(
        prefix='cxvpn-routing-service-', suffix='.result', delete=False)
    result_path = result.name
    result.close()
    parameters = subprocess.list2cmdline(
        [*arguments, '--result-file', result_path])

    class ShellExecuteInfo(ctypes.Structure):
        _fields_ = [
            ('cbSize', wintypes.DWORD), ('fMask', wintypes.ULONG),
            ('hwnd', wintypes.HWND), ('lpVerb', wintypes.LPCWSTR),
            ('lpFile', wintypes.LPCWSTR), ('lpParameters', wintypes.LPCWSTR),
            ('lpDirectory', wintypes.LPCWSTR), ('nShow', ctypes.c_int),
            ('hInstApp', wintypes.HINSTANCE), ('lpIDList', ctypes.c_void_p),
            ('lpClass', wintypes.LPCWSTR), ('hkeyClass', wintypes.HKEY),
            ('dwHotKey', wintypes.DWORD), ('hIconOrMonitor', wintypes.HANDLE),
            ('hProcess', wintypes.HANDLE),
        ]

    info = ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040
    info.lpVerb = 'runas'
    info.lpFile = path
    info.lpParameters = parameters
    info.nShow = 0
    shell32 = ctypes.WinDLL('shell32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(ShellExecuteInfo)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    try:
        if not shell32.ShellExecuteExW(ctypes.byref(info)):
            error = ctypes.get_last_error()
            if error == 1223:
                raise ServiceError('已取消 Windows 管理员授权，分流配置未变更')
            raise ServiceError(f'无法启动路由服务安装程序（Windows 错误 {error}）')
        wait = kernel32.WaitForSingleObject(
            info.hProcess, max(1000, int(timeout * 1000)))
        if wait == 0x00000102:
            raise ServiceError('Windows 路由服务安装超时')
        exit_code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(
            info.hProcess, ctypes.byref(exit_code))
        try:
            with open(result_path, encoding='utf-8-sig', errors='replace') as stream:
                child_result = stream.read(4096).strip()
        except OSError:
            child_result = ''
        if exit_code.value != 0:
            detail = child_result[6:] if child_result.startswith('ERROR:') else ''
            raise ServiceError(detail or '路由服务安装程序执行失败')
    finally:
        if info.hProcess:
            kernel32.CloseHandle(info.hProcess)
        try:
            os.remove(result_path)
        except OSError:
            pass


def _open_pipe(timeout_ms):
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    kernel32.WaitNamedPipeW.restype = wintypes.BOOL
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    if not kernel32.WaitNamedPipeW(PIPE_NAME, max(0, int(timeout_ms))):
        raise ServiceUnavailable('路由服务 IPC 未就绪')
    handle = kernel32.CreateFileW(
        PIPE_NAME, 0x80000000 | 0x40000000, 0, None, 3, 0, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ServiceUnavailable('无法连接路由服务 IPC')
    return handle


def _write_all(handle, data):
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.WriteFile.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    kernel32.WriteFile.restype = wintypes.BOOL
    offset = 0
    while offset < len(data):
        written = wintypes.DWORD()
        chunk = data[offset:offset + 1024 * 1024]
        buffer = ctypes.create_string_buffer(chunk)
        if not kernel32.WriteFile(
                handle, buffer, len(chunk), ctypes.byref(written), None):
            raise ServiceError('向路由服务发送请求失败')
        if not written.value:
            raise ServiceError('路由服务连接已中断')
        offset += written.value


def _read_exact(handle, length):
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.ReadFile.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    kernel32.ReadFile.restype = wintypes.BOOL
    chunks = []
    remaining = length
    while remaining:
        size = min(remaining, 1024 * 1024)
        buffer = ctypes.create_string_buffer(size)
        read = wintypes.DWORD()
        if not kernel32.ReadFile(
                handle, buffer, size, ctypes.byref(read), None):
            raise ServiceError('读取路由服务响应失败')
        if not read.value:
            raise ServiceError('路由服务连接已中断')
        chunks.append(buffer.raw[:read.value])
        remaining -= read.value
    return b''.join(chunks)
