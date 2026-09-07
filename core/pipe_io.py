# -*- coding: utf-8 -*-
"""Windows 重叠管道 I/O；同一请求共用期限，取消完成后才释放缓冲区。"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import time


class Overlapped(ctypes.Structure):
    _fields_ = [('Internal', ctypes.c_size_t), ('InternalHigh', ctypes.c_size_t),
                ('Offset', wintypes.DWORD), ('OffsetHigh', wintypes.DWORD),
                ('hEvent', wintypes.HANDLE)]


def transfer(handle, buffer, size, writing, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('路由服务响应超时')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                   wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateEventW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(Overlapped)]
    kernel.CancelIoEx.restype = wintypes.BOOL
    kernel.GetOverlappedResult.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(Overlapped),
        ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
    kernel.GetOverlappedResult.restype = wintypes.BOOL
    operation = kernel.WriteFile if writing else kernel.ReadFile
    operation.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                          ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(Overlapped)]
    operation.restype = wintypes.BOOL
    event = kernel.CreateEventW(None, True, False, None)
    if not event:
        raise OSError('无法创建路由服务 I/O 事件')
    overlapped = Overlapped(hEvent=event)
    count = wintypes.DWORD()
    try:
        if not operation(handle, buffer, size, ctypes.byref(count), ctypes.byref(overlapped)):
            if ctypes.get_last_error() != 997:  # ERROR_IO_PENDING
                raise OSError('路由服务管道读写失败')
            wait_ms = max(1, int((deadline - time.monotonic()) * 1000))
            outcome = kernel.WaitForSingleObject(event, wait_ms)
            if outcome != 0:
                kernel.CancelIoEx(handle, ctypes.byref(overlapped))
                # OVERLAPPED 与 buffer 在取消完成前必须保持存活。
                kernel.GetOverlappedResult(handle, ctypes.byref(overlapped),
                                           ctypes.byref(count), True)
                if outcome == 258:
                    raise TimeoutError('路由服务响应超时')
                raise OSError('等待路由服务响应失败')
            if not kernel.GetOverlappedResult(
                    handle, ctypes.byref(overlapped), ctypes.byref(count), False):
                raise OSError('路由服务连接已中断')
        return count.value
    finally:
        kernel.CloseHandle(event)
