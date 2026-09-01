# -*- coding: utf-8 -*-
"""Windows 桌面集成：当前用户开机自启与 WinForms 系统托盘。"""
import ctypes
from ctypes import wintypes
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
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
HOTKEY_ACTIONS = {
    1: ('toggle_proxy', MOD_CONTROL | MOD_ALT, ord('P')),
    2: ('toggle_mode', MOD_CONTROL | MOD_ALT, ord('M')),
    3: ('open_connections', MOD_CONTROL | MOD_ALT, ord('C')),
}


class _Point(ctypes.Structure):
    _fields_ = [('x', wintypes.LONG), ('y', wintypes.LONG)]


class _Message(ctypes.Structure):
    _fields_ = [
        ('hwnd', wintypes.HWND), ('message', wintypes.UINT),
        ('wParam', wintypes.WPARAM), ('lParam', wintypes.LPARAM),
        ('time', wintypes.DWORD), ('pt', _Point),
    ]


class GlobalHotkeyManager:
    """在独立消息线程注册固定、可审计的全局快捷键。"""

    def __init__(self, callbacks, log=print):
        self.callbacks = callbacks or {}
        self.log = log
        self._thread = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._registered_count = 0

    def start(self, enabled=True):
        if not enabled or os.name != 'nt':
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._run, name='global-hotkeys', daemon=True)
        self._thread.start()
        self._ready.wait(2)
        return self._registered_count == len(self.callbacks)

    def stop(self):
        thread = self._thread
        thread_id = self._thread_id
        if thread and thread.is_alive() and thread_id:
            try:
                user32 = ctypes.WinDLL('user32', use_last_error=True)
                user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)
            except OSError:
                pass
            thread.join(timeout=3)
        self._thread = None
        self._thread_id = 0
        self._registered_count = 0

    def restart(self, enabled):
        self.stop()
        return self.start(bool(enabled))

    def _run(self):
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        user32.RegisterHotKey.argtypes = [
            wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetMessageW.argtypes = [
            ctypes.POINTER(_Message), wintypes.HWND, wintypes.UINT,
            wintypes.UINT]
        user32.GetMessageW.restype = ctypes.c_int
        self._thread_id = kernel32.GetCurrentThreadId()
        registered = []
        try:
            for hotkey_id, (action, modifiers, key) in HOTKEY_ACTIONS.items():
                if action in self.callbacks and user32.RegisterHotKey(
                        None, hotkey_id, modifiers, key):
                    registered.append(hotkey_id)
                elif action in self.callbacks:
                    self.log(f'[desktop] 全局快捷键注册失败: {action}')
            self._registered_count = len(registered)
            if len(registered) != len(self.callbacks):
                for hotkey_id in registered:
                    user32.UnregisterHotKey(None, hotkey_id)
                registered.clear()
                self._registered_count = 0
            self._ready.set()
            if registered:
                self.log('[desktop] 全局快捷键已启用: Ctrl+Alt+P/M/C')
            message = _Message()
            while registered:
                status = user32.GetMessageW(
                    ctypes.byref(message), None, 0, 0)
                if status <= 0:
                    break
                if message.message != WM_HOTKEY:
                    continue
                action = HOTKEY_ACTIONS.get(int(message.wParam), ('', 0, 0))[0]
                callback = self.callbacks.get(action)
                if callback:
                    try:
                        callback()
                    except Exception as exc:
                        self.log(
                            f'[desktop] 全局快捷键执行失败: '
                            f'{type(exc).__name__}')
        finally:
            for hotkey_id in registered:
                user32.UnregisterHotKey(None, hotkey_id)
            self._thread_id = 0
            self._registered_count = 0
            self._ready.set()


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
                 quick_snapshot=None, quick_actions=None, hotkeys_enabled=None,
                 on_open_page=None, on_action_complete=None, log=print):
        self.window = window
        self.close_to_tray = close_to_tray
        self.on_exit = on_exit
        self._on_os_shutdown = on_os_shutdown
        self._quick_snapshot = quick_snapshot or (lambda: {})
        self._quick_actions = quick_actions or {}
        self._hotkeys_enabled = hotkeys_enabled or (lambda: False)
        self._on_open_page = on_open_page or (lambda _page: None)
        self._on_action_complete = on_action_complete or (lambda: None)
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
        self._menu_item_type = None
        self._exiting = False
        self._started = False
        self._hotkeys_requested = None
        self._tray_items = {}
        self._hotkeys = GlobalHotkeyManager({
            'toggle_proxy': lambda: self._run_quick_action(
                'toggle_proxy', '代理状态'),
            'toggle_mode': lambda: self._run_quick_action(
                'toggle_mode', '流量模式'),
            'open_connections': lambda: self.open_page('connections'),
        }, log=self.log)

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
        self._menu_item_type = ToolStripMenuItem

        def setup():
            if self._started:
                return
            notify = NotifyIcon()
            self._icon = self._native.Icon.Clone()
            notify.Icon = self._icon
            notify.Text = WINDOW_TITLE
            menu = ContextMenuStrip()
            status_item = ToolStripMenuItem('正在读取代理状态')
            status_item.Enabled = False
            toggle_item = ToolStripMenuItem('开启代理')
            toggle_item.Click += self._on_toggle_proxy
            mode_menu = ToolStripMenuItem('流量模式')
            rule_item = ToolStripMenuItem('规则模式')
            global_item = ToolStripMenuItem('全局模式')
            rule_item.Tag = 'rule'
            global_item.Tag = 'global'
            rule_item.Click += self._on_set_mode
            global_item.Click += self._on_set_mode
            mode_menu.DropDownItems.Add(rule_item)
            mode_menu.DropDownItems.Add(global_item)
            nodes_menu = ToolStripMenuItem('切换节点')
            connections_item = ToolStripMenuItem('打开连接页')
            connections_item.Click += self._on_open_connections
            show_item = ToolStripMenuItem('打开主窗口')
            exit_item = ToolStripMenuItem(f'退出 {WINDOW_TITLE}')
            show_item.Click += self._on_show
            exit_item.Click += self._on_exit
            menu.Items.Add(status_item)
            menu.Items.Add(toggle_item)
            menu.Items.Add(mode_menu)
            menu.Items.Add(nodes_menu)
            menu.Items.Add(connections_item)
            menu.Items.Add(ToolStripSeparator())
            menu.Items.Add(show_item)
            menu.Items.Add(ToolStripSeparator())
            menu.Items.Add(exit_item)
            notify.ContextMenuStrip = menu
            menu.Opening += self._on_menu_opening
            notify.MouseClick += self._on_mouse_click
            self._native.FormClosing += self._on_form_closing
            notify.Visible = True
            self._notify = notify
            self._menu = menu
            self._tray_items = {
                'status': status_item, 'toggle': toggle_item,
                'rule': rule_item, 'global': global_item,
                'nodes': nodes_menu,
            }
            self._started = True
            self.log('[desktop] 系统托盘已启动')

        self._invoke(setup, wait=True)
        self.refresh_hotkeys()

    def refresh_hotkeys(self):
        try:
            enabled = bool(self._hotkeys_enabled())
        except Exception:
            enabled = False
        if enabled == self._hotkeys_requested:
            return (not enabled or
                    self._hotkeys._registered_count == len(
                        self._hotkeys.callbacks))
        self._hotkeys_requested = enabled
        active = self._hotkeys.restart(enabled)
        if enabled and not active and self._notify:
            self.notify(
                '全局快捷键未完全启用',
                'Ctrl+Alt+P/M/C 中有按键被其它程序占用，请关闭冲突程序后重新开启')
        return active

    @property
    def hotkeys_active(self):
        return bool(
            self._hotkeys_requested and
            self._hotkeys._registered_count == len(self._hotkeys.callbacks))

    def _on_menu_opening(self, _sender, _args):
        try:
            snapshot = self._quick_snapshot() or {}
        except Exception as exc:
            self.log(f'[desktop] 托盘状态读取失败: {type(exc).__name__}')
            snapshot = {}
        enabled = bool(snapshot.get('enabled'))
        self._tray_items['status'].Text = (
            f'代理：{"已开启" if enabled else "未开启"} · '
            f'{"全局" if snapshot.get("traffic_mode") == "global" else "规则"}模式')
        self._tray_items['toggle'].Text = '关闭代理' if enabled else '开启代理'
        self._tray_items['rule'].Checked = snapshot.get('traffic_mode') != 'global'
        self._tray_items['global'].Checked = snapshot.get('traffic_mode') == 'global'
        nodes_menu = self._tray_items['nodes']
        nodes_menu.DropDownItems.Clear()
        providers = snapshot.get('providers') or []
        nodes_menu.Enabled = bool(providers)
        for provider in providers:
            submenu = self._menu_item_type(
                str(provider.get('name') or '代理订阅'))
            for node in provider.get('nodes') or []:
                item = self._menu_item_type(
                    str(node.get('name') or '未命名节点'))
                item.Tag = f'{provider.get("id")}\0{node.get("name")}'
                item.Checked = bool(node.get('selected'))
                item.Click += self._on_select_node
                submenu.DropDownItems.Add(item)
            nodes_menu.DropDownItems.Add(submenu)

    def _run_quick_action(self, action, label, *args):
        callback = self._quick_actions.get(action)
        if not callback:
            return

        def run():
            try:
                result = callback(*args) or {}
                self.notify(
                    f'{label}{"成功" if result.get("ok") else "失败"}',
                    result.get('msg') or '操作已完成')
                if result.get('ok'):
                    self._on_action_complete()
            except Exception as exc:
                self.log(
                    f'[desktop] 托盘快捷操作失败: {type(exc).__name__}')
                self.notify(f'{label}失败', '操作未完成，请打开主窗口查看日志')

        threading.Thread(
            target=run, name=f'tray-{action}', daemon=True).start()

    def _on_toggle_proxy(self, _sender=None, _args=None):
        self._run_quick_action('toggle_proxy', '代理切换')

    def _on_set_mode(self, sender, _args):
        self._run_quick_action('set_mode', '流量模式切换', str(sender.Tag))

    def _on_select_node(self, sender, _args):
        provider_id, node_name = str(sender.Tag).split('\0', 1)
        self._run_quick_action(
            'select_node', '节点切换', provider_id, node_name)

    def _on_open_connections(self, _sender=None, _args=None):
        self.open_page('connections')

    def open_page(self, page):
        target = str(page or 'overview')

        def navigate():
            self._show_native()
            try:
                self._on_open_page(target)
            except Exception as exc:
                self.log(
                    f'[desktop] 页面快捷入口失败: {type(exc).__name__}')

        self._invoke(navigate)

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
        self._hotkeys.stop()
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
        self._hotkeys.stop()
        self._invoke(self._dispose_native, wait=True)
