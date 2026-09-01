# -*- coding: utf-8 -*-
"""统一分流的文件完整性与错误脱敏工具。"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys


SENSITIVE_URL_RE = re.compile(r'https?://[^\s\'"<>]+', re.I)
SENSITIVE_FIELD_RE = re.compile(
    r'(?i)[\'\"]?(authorization|bearer|token|secret|password|passwd|api[-_]?key)'
    r'[\'\"]?(?:\s*[:=]\s*|\s+)(?:bearer\s+)?[\'\"]?'
    r'[^\s,;\]}}\'\"]+')


def runtime_dir():
    if getattr(sys, 'frozen', False):
        root = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
    else:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, 'runtime', 'routing')


def binary_path(name):
    return os.path.join(runtime_dir(), name)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest().upper()


def verify_runtime_files(runtime_dir, expected, error_type):
    for name, checksum in expected.items():
        path = os.path.join(runtime_dir, name)
        if not os.path.isfile(path):
            raise error_type(f'统一分流运行时缺失：{name}')
        if sha256_file(path) != checksum:
            raise error_type(f'统一分流运行时校验失败：{name}')
    return True


def sanitize_mihomo_error(value):
    """保留可操作的 Mihomo 错误，同时移除 URL、凭据和控制字符。"""
    text = str(value or '').replace('\x00', '').strip()
    text = SENSITIVE_URL_RE.sub('[已隐藏 URL]', text)
    text = SENSITIVE_FIELD_RE.sub(
        lambda match: f'{match.group(1)}=[已隐藏]', text)
    text = ''.join(char if char in '\t ' or ord(char) >= 32 else ' '
                   for char in text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:500] or '未知错误'


def provider_preview_error(log_text, provider_seen):
    """把 Mihomo 订阅日志归类为不泄漏订阅 URL 的用户提示。"""
    text = str(log_text or '').lower()
    if any(token in text for token in ('401', '403', 'unauthorized', 'forbidden')):
        return '订阅服务器拒绝访问，请确认订阅未过期且允许 Clash/Mihomo 客户端访问'
    if any(token in text for token in ('certificate', 'x509', 'tls')):
        return '订阅服务器的 HTTPS 证书校验失败'
    if 'timeout' in text or 'deadline exceeded' in text:
        return '订阅服务器响应超时，请检查当前网络后重试'
    if any(token in text for token in (
            'yaml', 'proxy 0', 'unsupported proxy', 'format', 'decode')):
        return '订阅内容无法解析，请确认地址返回 Clash/Mihomo 节点格式而不是网页或登录页'
    if provider_seen:
        return '订阅已连接但没有解析出节点，请检查包含/排除筛选条件和订阅内容'
    return '无法获取订阅，请检查地址、当前网络和订阅有效期后重试'


def attach_kill_on_close_job(process):
    """把临时子进程放入 Job；主程序异常退出时由 Windows 自动终止。"""
    import ctypes
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ('ReadOperationCount', ctypes.c_uint64),
            ('WriteOperationCount', ctypes.c_uint64),
            ('OtherOperationCount', ctypes.c_uint64),
            ('ReadTransferCount', ctypes.c_uint64),
            ('WriteTransferCount', ctypes.c_uint64),
            ('OtherTransferCount', ctypes.c_uint64),
        ]

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ('PerProcessUserTimeLimit', ctypes.c_int64),
            ('PerJobUserTimeLimit', ctypes.c_int64),
            ('LimitFlags', wintypes.DWORD),
            ('MinimumWorkingSetSize', ctypes.c_size_t),
            ('MaximumWorkingSetSize', ctypes.c_size_t),
            ('ActiveProcessLimit', wintypes.DWORD),
            ('Affinity', ctypes.c_size_t),
            ('PriorityClass', wintypes.DWORD),
            ('SchedulingClass', wintypes.DWORD),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ('BasicLimitInformation', BasicLimitInformation),
            ('IoInfo', IoCounters),
            ('ProcessMemoryLimit', ctypes.c_size_t),
            ('JobMemoryLimit', ctypes.c_size_t),
            ('PeakProcessMemoryUsed', ctypes.c_size_t),
            ('PeakJobMemoryUsed', ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        limits = ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = 0x00002000
        if not kernel32.SetInformationJobObject(
                job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel32.AssignProcessToJobObject(
                job, wintypes.HANDLE(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())
        return lambda: kernel32.CloseHandle(job)
    except Exception:
        kernel32.CloseHandle(job)
        raise


def stop_temporary_process(process, close_job=None):
    """可靠回收临时进程，并在正常终止超时后强制结束。"""
    stopped = process.poll() is not None
    try:
        if not stopped:
            process.terminate()
            try:
                process.wait(timeout=3)
                stopped = True
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=3)
                    stopped = True
                except subprocess.TimeoutExpired:
                    pass
    finally:
        if close_job:
            close_job()
    if not stopped:
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
