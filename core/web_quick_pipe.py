# -*- coding: utf-8 -*-
"""网页快开宿主与主程序之间的本机命名管道：仅当前用户可访问，逐条处理定长帧 JSON 消息。"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
import struct
import sys
import threading
import time

from core import pipe_io, routing_service

PIPE_NAME = r'\\.\pipe\CXVPNTools.WebQuick.v1'
MAX_MESSAGE = 64 * 1024
IO_SECONDS = 10
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
ERROR_FILE_NOT_FOUND = 2
ERROR_PIPE_BUSY = 231
ERROR_IO_PENDING = 997
ERROR_PIPE_CONNECTED = 535
_library = None


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [('nLength', wintypes.DWORD), ('lpSecurityDescriptor', ctypes.c_void_p),
                ('bInheritHandle', wintypes.BOOL)]


def _kernel32():
    global _library
    if _library is not None:
        return _library
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    overlapped = ctypes.POINTER(pipe_io.Overlapped)
    dword = ctypes.POINTER(wintypes.DWORD)
    kernel.CreateNamedPipeW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_SecurityAttributes)]
    kernel.CreateNamedPipeW.restype = wintypes.HANDLE
    kernel.ConnectNamedPipe.argtypes = [wintypes.HANDLE, overlapped]
    kernel.ConnectNamedPipe.restype = wintypes.BOOL
    kernel.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
    kernel.DisconnectNamedPipe.restype = wintypes.BOOL
    kernel.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateEventW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.GetOverlappedResult.argtypes = [wintypes.HANDLE, overlapped, dword, wintypes.BOOL]
    kernel.GetOverlappedResult.restype = wintypes.BOOL
    kernel.GetNamedPipeClientProcessId.argtypes = [wintypes.HANDLE, dword]
    kernel.GetNamedPipeClientProcessId.restype = wintypes.BOOL
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, dword]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    kernel.WaitNamedPipeW.restype = wintypes.BOOL
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    _library = kernel
    return kernel


def _read_exact(handle, size, deadline):
    chunks = []
    while size:
        buffer = ctypes.create_string_buffer(size)
        read = pipe_io.transfer(handle, buffer, size, False, deadline)
        if not read:
            raise ConnectionError('pipe closed')
        chunks.append(buffer.raw[:read])
        size -= read
    return b''.join(chunks)


def send(handle, value, deadline):
    body = json.dumps(value, ensure_ascii=False).encode('utf-8')
    data = struct.pack('<I', len(body)) + body
    offset = 0
    while offset < len(data):
        chunk = data[offset:offset + MAX_MESSAGE]
        buffer = ctypes.create_string_buffer(chunk)
        written = pipe_io.transfer(handle, buffer, len(chunk), True, deadline)
        if not written:
            raise ConnectionError('pipe closed')
        offset += written


def receive(handle, deadline):
    (length,) = struct.unpack('<I', _read_exact(handle, 4, deadline))
    if not 0 < length <= MAX_MESSAGE:
        raise ValueError('frame out of range')
    return json.loads(_read_exact(handle, length, deadline).decode('utf-8'))


def acknowledge(handle, deadline):
    """响应读取完毕后写 1 字节确认：服务端收到后才断开，断开会丢弃客户端未读的数据。"""
    buffer = ctypes.create_string_buffer(b'\n')
    pipe_io.transfer(handle, buffer, 1, True, deadline)


def request(name, value, timeout_ms):
    """客户端：连接、发送一条消息、读取响应并确认后关闭。"""
    kernel = _kernel32()
    handle = connect(name, timeout_ms)
    try:
        deadline = time.monotonic() + IO_SECONDS + 2
        send(handle, value, deadline)
        response = receive(handle, deadline)
        acknowledge(handle, deadline)
        return response
    finally:
        kernel.CloseHandle(handle)


def connect(name, timeout_ms):
    """连接服务端；期限内始终连不上时抛出 OSError。"""
    kernel = _kernel32()
    deadline = time.monotonic() + max(0, timeout_ms) / 1000
    while True:
        remaining = max(0, int((deadline - time.monotonic()) * 1000))
        if kernel.WaitNamedPipeW(name, remaining):
            handle = kernel.CreateFileW(name, 0x80000000 | 0x40000000, 0, None, 3, 0x40000000, None)
            if handle != INVALID_HANDLE_VALUE:
                return handle
        if ctypes.get_last_error() not in (ERROR_FILE_NOT_FOUND, ERROR_PIPE_BUSY):
            raise ctypes.WinError(ctypes.get_last_error())
        if time.monotonic() >= deadline:
            raise ctypes.WinError(ctypes.get_last_error())
        time.sleep(0.05)


class PipeServer:
    """单实例管道；只接受同一可执行文件启动的宿主进程，每次连接处理一条消息。"""

    def __init__(self, handler, log, name=PIPE_NAME):
        self._handler = handler
        self._log = log
        self._name = name
        self._kernel = _kernel32()
        self._advapi32 = ctypes.WinDLL('advapi32', use_last_error=True)
        self._advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.DWORD)]
        self._advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
        self._kernel.LocalFree.argtypes = [ctypes.c_void_p]
        self._kernel.LocalFree.restype = ctypes.c_void_p
        self._stopping = threading.Event()
        self._thread = None
        # 虚拟环境中 sys.executable 是启动器，宿主进程的真实映像是基础解释器，须按真实映像比较。
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(1024)
        self._expected_image = os.path.normcase(os.path.realpath(sys.executable))
        if self._kernel.QueryFullProcessImageNameW(
                ctypes.c_void_p(-1), 0, buffer, ctypes.byref(size)):
            self._expected_image = os.path.normcase(os.path.realpath(buffer.value))

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._serve, name='web-quick-pipe', daemon=True)
        self._thread.start()

    def stop(self):
        """用一次空连接唤醒等待中的 accept；线程为守护线程，不强求其退出。"""
        self._stopping.set()
        thread = self._thread
        if thread is None or not thread.is_alive():
            return
        for _ in range(10):
            try:
                handle = connect(self._name, 150)
            except OSError:
                if not thread.is_alive():
                    break
                continue
            self._kernel.CloseHandle(handle)
            break
        thread.join(timeout=2)

    def _create(self, owner_sid):
        # 与路由服务一致：仅 SYSTEM、管理员与当前用户可访问，并拒绝远程客户端。
        sddl = f'D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GRGW;;;{owner_sid})'
        descriptor = ctypes.c_void_p()
        if not self._advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, 1, ctypes.byref(descriptor), None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            # PIPE_ACCESS_DUPLEX | FILE_FLAG_OVERLAPPED | FILE_FLAG_FIRST_PIPE_INSTANCE，
            # PIPE_REJECT_REMOTE_CLIENTS：类型与读模式均为字节、阻塞等待。
            attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
            handle = self._kernel.CreateNamedPipeW(
                self._name, 0x00000003 | 0x40000000 | 0x00080000, 0x00000008, 1,
                MAX_MESSAGE, MAX_MESSAGE, 0, ctypes.byref(attributes))
        finally:
            self._kernel.LocalFree(descriptor)
        if handle == INVALID_HANDLE_VALUE:
            raise ctypes.WinError(ctypes.get_last_error())
        return handle

    def _accept(self, handle):
        event = self._kernel.CreateEventW(None, True, False, None)
        overlapped = pipe_io.Overlapped(hEvent=event)
        moved = wintypes.DWORD()
        try:
            if not self._kernel.ConnectNamedPipe(handle, ctypes.byref(overlapped)):
                error = ctypes.get_last_error()
                if error != ERROR_IO_PENDING:
                    return error == ERROR_PIPE_CONNECTED
            if self._kernel.WaitForSingleObject(event, 0xFFFFFFFF) != 0:
                return False
            return bool(self._kernel.GetOverlappedResult(
                handle, ctypes.byref(overlapped), ctypes.byref(moved), False))
        finally:
            self._kernel.CloseHandle(event)

    def _peer_image(self, handle):
        process_id = wintypes.DWORD()
        if not self._kernel.GetNamedPipeClientProcessId(handle, ctypes.byref(process_id)):
            return None
        process = self._kernel.OpenProcess(0x1000, False, process_id.value)
        if not process:
            return None
        try:
            size = wintypes.DWORD(1024)
            buffer = ctypes.create_unicode_buffer(1024)
            if self._kernel.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
                return buffer.value
            return None
        finally:
            self._kernel.CloseHandle(process)

    def _handle(self, handle):
        if self._stopping.is_set():
            return
        deadline = time.monotonic() + IO_SECONDS
        peer = self._peer_image(handle)
        if peer is None or os.path.normcase(os.path.realpath(peer)) != self._expected_image:
            self._log('[web-quick] 自动登录管道拒绝连接：来源不是本程序的宿主进程')
            self._reply(handle, {'ok': False, 'error': 'forbidden'}, deadline)
            return
        request = receive(handle, deadline)
        started = time.monotonic()
        kind = request.get('type') if isinstance(request, dict) else '无效'
        self._log(f'[web-quick] 自动登录管道收到 {kind} 请求，后台已领取并开始执行；期限 {IO_SECONDS} 秒')
        try:
            response = self._handler(request)
        except Exception as exc:
            self._log(f'[web-quick] 自动登录管道请求处理失败：{type(exc).__name__}')
            response = {'ok': False, 'error': 'internal_error'}
        outcome = response.get('error', 'ok') if isinstance(response, dict) else 'invalid'
        self._log(f'[web-quick] 自动登录管道请求处理完成：{outcome}，耗时 {time.monotonic() - started:.2f} 秒')
        self._reply(handle, response, time.monotonic() + 5)

    def _reply(self, handle, response, deadline):
        send(handle, response, deadline)
        buffer = ctypes.create_string_buffer(1)
        try:
            pipe_io.transfer(handle, buffer, 1, False, time.monotonic() + 2)
        except (OSError, TimeoutError):
            pass

    def _serve(self):
        try:
            owner_sid = routing_service.current_user_sid()
        except Exception as exc:
            self._log(f'[web-quick] 自动登录管道未启动：无法读取当前用户身份（{type(exc).__name__}）')
            return
        while not self._stopping.is_set():
            try:
                handle = self._create(owner_sid)
            except OSError as exc:
                self._log(f'[web-quick] 自动登录管道未启动：创建失败（错误 {getattr(exc, "winerror", None)}）')
                return
            try:
                if self._accept(handle) and not self._stopping.is_set():
                    self._handle(handle)
            except Exception as exc:
                self._log(f'[web-quick] 自动登录管道连接中断：{type(exc).__name__}')
            finally:
                self._kernel.DisconnectNamedPipe(handle)
                self._kernel.CloseHandle(handle)
