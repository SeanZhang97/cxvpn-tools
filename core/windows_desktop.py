# -*- coding: utf-8 -*-
"""Windows 桌面集成：当前用户开机自启与 WinForms 系统托盘。"""
import ctypes
import os
import subprocess
import sys
import threading
import time
import winreg


APP_NAME = 'CXVPN管理器'
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
INSTANCE_MUTEX = r'Local\CXVPNManager.Singleton.v1'
WINDOW_TITLE = 'CX VPN TOOLS'
ERROR_ALREADY_EXISTS = 183


class SingleInstanceGuard:
    """使用 Windows 命名互斥保证同一用户会话只运行一个实例。"""

    def __init__(self, name=INSTANCE_MUTEX, kernel32=None):
        self.name = name
        self._handle = None
        if kernel32 is None:
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel32.CreateMutexW.argtypes = [
                ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_bool
            self._get_last_error = ctypes.get_last_error
        else:
            self._get_last_error = kernel32.GetLastError
        self._kernel32 = kernel32

    def acquire(self):
        if self._handle:
            return True
        handle = self._kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            raise ctypes.WinError(self._get_last_error())
        if self._get_last_error() == ERROR_ALREADY_EXISTS:
            self._kernel32.CloseHandle(handle)
            return False
        self._handle = handle
        return True

    def close(self):
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def activate_existing_window(title=WINDOW_TITLE, user32=None, attempts=30,
                             delay=0.1):
    """唤醒已运行实例；首次实例仍在创建窗口时会短暂重试。"""
    if user32 is None:
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        user32.FindWindowW.restype = ctypes.c_void_p
        user32.IsIconic.argtypes = [ctypes.c_void_p]
        user32.IsIconic.restype = ctypes.c_bool
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.ShowWindow.restype = ctypes.c_bool
        user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        user32.SetForegroundWindow.restype = ctypes.c_bool
    for _ in range(max(1, attempts)):
        hwnd = user32.FindWindowW(None, title)
        if hwnd:
            user32.ShowWindow(hwnd, 5)  # SW_SHOW
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            return True
        if delay > 0:
            time.sleep(delay)
    return False


def startup_command(executable=None, app_base=None, frozen=None):
    """返回当前安装位置对应的登录启动命令。"""
    executable = executable or sys.executable
    app_base = app_base or os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))
    frozen = getattr(sys, 'frozen', False) if frozen is None else frozen
    args = [executable]
    if not frozen:
        args.append(os.path.join(app_base, 'main.py'))
    args.append('--startup')
    return subprocess.list2cmdline(args)


def read_startup_command(registry=winreg):
    try:
        key = registry.OpenKey(
            registry.HKEY_CURRENT_USER, RUN_KEY, 0, registry.KEY_READ)
    except OSError:
        return ''
    try:
        value, _ = registry.QueryValueEx(key, APP_NAME)
        return str(value or '')
    except OSError:
        return ''
    finally:
        registry.CloseKey(key)


def is_startup_enabled(registry=winreg, command=None):
    """注册表必须指向当前程序路径；移动目录后的旧值不算已启用。"""
    current = read_startup_command(registry).strip()
    expected = (command or startup_command()).strip()
    return bool(current) and os.path.normcase(current) == os.path.normcase(expected)


def set_startup_enabled(enabled, registry=winreg, command=None):
    """写入当前用户 Run 项，无需管理员权限。"""
    if enabled:
        key = registry.CreateKeyEx(
            registry.HKEY_CURRENT_USER, RUN_KEY, 0, registry.KEY_SET_VALUE)
        try:
            registry.SetValueEx(
                key, APP_NAME, 0, registry.REG_SZ,
                command or startup_command())
        finally:
            registry.CloseKey(key)
        return
    try:
        key = registry.OpenKey(
            registry.HKEY_CURRENT_USER, RUN_KEY, 0, registry.KEY_SET_VALUE)
    except OSError:
        return
    try:
        try:
            registry.DeleteValue(key, APP_NAME)
        except FileNotFoundError:
            pass
    finally:
        registry.CloseKey(key)


def should_hide_to_tray(close_reason, exiting=False, enabled=True):
    """仅拦截用户点击关闭；关机、注销和显式退出必须正常结束。"""
    return bool(enabled and not exiting and str(close_reason) == 'UserClosing')


class DesktopController:
    """依托 pywebview 已加载的 WinForms 消息循环维护 NotifyIcon。"""

    _serializable = False

    def __init__(self, window, close_to_tray, on_exit, on_os_shutdown=None,
                 log=print):
        self.window = window
        self.close_to_tray = close_to_tray
        self.on_exit = on_exit
        self._on_os_shutdown = on_os_shutdown
        self.log = log
        self._native = None
        self._notify = None
        self._menu = None
        self._icon = None
        self._action_type = None
        self._window_state = None
        self._close_reason = None
        self._tooltip_icon = None
        self._mouse_buttons = None
        self._exiting = False
        self._started = False

    def start(self):
        if self._started or os.name != 'nt' or self.window.native is None:
            return
        import clr
        clr.AddReference('System')
        clr.AddReference('System.Drawing')
        clr.AddReference('System.Windows.Forms')
        from System import Action
        from System.Windows.Forms import (
            CloseReason, ContextMenuStrip, FormWindowState, MouseButtons,
            NotifyIcon,
            ToolStripMenuItem, ToolStripSeparator, ToolTipIcon)

        self._native = self.window.native
        self._action_type = Action
        self._window_state = FormWindowState
        self._close_reason = CloseReason
        self._tooltip_icon = ToolTipIcon
        self._mouse_buttons = MouseButtons

        def setup():
            if self._started:
                return
            notify = NotifyIcon()
            self._icon = self._native.Icon.Clone()
            notify.Icon = self._icon
            notify.Text = WINDOW_TITLE
            menu = ContextMenuStrip()
            show_item = ToolStripMenuItem('打开主窗口')
            exit_item = ToolStripMenuItem(f'退出 {WINDOW_TITLE}')
            show_item.Click += self._on_show
            exit_item.Click += self._on_exit
            menu.Items.Add(show_item)
            menu.Items.Add(ToolStripSeparator())
            menu.Items.Add(exit_item)
            notify.ContextMenuStrip = menu
            notify.MouseClick += self._on_mouse_click
            self._native.FormClosing += self._on_form_closing
            notify.Visible = True
            self._notify = notify
            self._menu = menu
            self._started = True
            self.log('[desktop] 系统托盘已启动')

        self._invoke(setup, wait=True)

    def _invoke(self, action, wait=False):
        native = self._native
        if native is None or native.IsDisposed:
            return
        delegate = self._action_type(action)
        if native.InvokeRequired:
            if wait:
                native.Invoke(delegate)
            else:
                native.BeginInvoke(delegate)
        else:
            action()

    def _show_native(self):
        if self._native.WindowState == self._window_state.Minimized:
            self._native.WindowState = self._window_state.Normal
        self._native.Show()
        self._native.Activate()

    def show_window(self):
        self._invoke(self._show_native)

    def hide_window(self):
        def hide():
            self._native.Hide()
        self._invoke(hide)

    def notify(self, title, message):
        """仅发送托盘气泡，不弹窗，供启动自检等不需要打扰用户的提示。"""
        def balloon():
            if not self._notify:
                return
            self._notify.BalloonTipTitle = str(title)[:63]
            self._notify.BalloonTipText = str(message)[:220]
            self._notify.BalloonTipIcon = self._tooltip_icon.Info
            self._notify.ShowBalloonTip(5000)
        self._invoke(balloon)

    def request_attention(self, title, message):
        """人工验证码等需要用户参与时，恢复窗口并发送托盘提示。"""
        self.notify(title, message)
        self._invoke(self._show_native)

    def _on_show(self, _sender, _args):
        self.show_window()

    def _on_mouse_click(self, _sender, args):
        if args.Button == self._mouse_buttons.Left:
            self.show_window()

    def _on_exit(self, _sender, _args):
        if self._exiting:
            return
        self._exiting = True
        self._dispose_native()
        threading.Thread(target=self.on_exit, daemon=True).start()

    def _on_form_closing(self, _sender, args):
        enabled = True
        try:
            enabled = bool(self.close_to_tray())
        except Exception:
            pass
        if should_hide_to_tray(args.CloseReason, self._exiting, enabled):
            args.Cancel = True
            self.hide_window()
            return
        # 走到这里表示窗口真的在关闭。系统关机/注销/任务管理器结束前，
        # 先同步清扫本地代理残留（毫秒级注册表操作），再放行退出。
        if str(args.CloseReason) in ('WindowsShutDown', 'TaskManagerClosing'):
            try:
                if self._on_os_shutdown:
                    self._on_os_shutdown()
            except Exception:
                pass

    def _dispose_native(self):
        if self._notify:
            self._notify.Visible = False
            self._notify.Dispose()
            self._notify = None
        if self._menu:
            self._menu.Dispose()
            self._menu = None
        if self._icon:
            self._icon.Dispose()
            self._icon = None

    def dispose(self):
        self._exiting = True
        self._invoke(self._dispose_native, wait=True)
