# -*- coding: utf-8 -*-
"""Edge 内部页启动：新窗口识别、最大化和焦点确认后的固定键盘导航。"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import time

NAVIGATE_SECONDS = 6.0
_KEYBOARD = 1
_KEYUP = 0x0002
_UNICODE = 0x0004
_CONTROL = 0x11
_RETURN = 0x0D


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [('wVk', wintypes.WORD), ('wScan', wintypes.WORD),
                ('dwFlags', wintypes.DWORD), ('time', wintypes.DWORD),
                ('dwExtraInfo', ctypes.c_size_t)]


class _INPUT_VALUE(ctypes.Union):
    _fields_ = [('ki', _KEYBDINPUT),
                ('padding', wintypes.BYTE * (32 if ctypes.sizeof(ctypes.c_void_p) == 8 else 24))]


class _INPUT(ctypes.Structure):
    _fields_ = [('type', wintypes.DWORD), ('value', _INPUT_VALUE)]


def _user32():
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    return user32


def _kernel32():
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    return kernel32


def _process_path(user32, hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    kernel32 = _kernel32()
    process = kernel32.OpenProcess(0x1000, False, pid.value)
    if not process:
        return ''
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
            return ''
        return os.path.normcase(os.path.abspath(buffer.value))
    finally:
        kernel32.CloseHandle(process)


def browser_windows(executable=None):
    """返回当前顶层 Edge 窗口句柄；按真实进程映像过滤，避免操作 WebView。"""
    user32 = _user32()
    expected = os.path.normcase(os.path.abspath(executable)) if executable else ''
    windows = set()
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd, _):
        name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, name, len(name))
        if name.value == 'Chrome_WidgetWin_1' and (
                not expected or _process_path(user32, hwnd) == expected):
            windows.add(hwnd)
        return True

    user32.EnumWindows(callback_type(visit), 0)
    return windows


def _keyboard_events(url):
    events = []

    def add(vk, scan, flags):
        item = _INPUT()
        item.type = _KEYBOARD
        item.value.ki = _KEYBDINPUT(vk, scan, flags, 0, 0)
        events.append(item)

    add(_CONTROL, 0, 0)
    add(ord('L'), 0, 0)
    add(ord('L'), 0, _KEYUP)
    add(_CONTROL, 0, _KEYUP)
    for character in url:
        add(0, ord(character), _UNICODE)
        add(0, ord(character), _UNICODE | _KEYUP)
    add(_RETURN, 0, 0)
    add(_RETURN, 0, _KEYUP)
    return events


def _activate_window(user32, hwnd, maximized):
    current = _kernel32().GetCurrentThreadId()
    target = user32.GetWindowThreadProcessId(hwnd, None)
    attached = current != target and bool(user32.AttachThreadInput(current, target, True))
    try:
        if maximized:
            user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
        time.sleep(0.08)
        return user32.GetForegroundWindow() == hwnd
    finally:
        if attached:
            user32.AttachThreadInput(current, target, False)


def navigate_new_window(previous, executable, url, timeout=NAVIGATE_SECONDS, maximized=False):
    """只向本次新增且获得焦点的 Edge 窗口输入固定地址。"""
    user32 = _user32()
    deadline = time.monotonic() + timeout
    hwnd = None
    while time.monotonic() < deadline:
        candidates = browser_windows(executable) - set(previous)
        foreground = user32.GetForegroundWindow()
        if foreground in candidates:
            hwnd = foreground
            break
        if len(candidates) == 1:
            hwnd = next(iter(candidates))
            break
        time.sleep(0.05)
    if hwnd is None:
        raise TimeoutError('等待 Edge 新窗口超时')
    if not _activate_window(user32, hwnd, maximized):
        raise OSError('Edge 新窗口未获得焦点')
    events = _keyboard_events(url)
    payload = (_INPUT * len(events))(*events)
    if user32.SendInput(len(events), payload, ctypes.sizeof(_INPUT)) != len(events):
        raise OSError('未能输入 Edge 内部地址')
