# -*- coding: utf-8 -*-
import unittest
from types import SimpleNamespace
from unittest import mock

from core import windows_desktop


class _FakeRegistry:
    HKEY_CURRENT_USER = object()
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.values = {}

    def OpenKey(self, _root, _path, _reserved, _access):
        return object()

    def CreateKeyEx(self, _root, _path, _reserved, _access):
        return object()

    def QueryValueEx(self, _key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], self.REG_SZ

    def SetValueEx(self, _key, name, _reserved, _kind, value):
        self.values[name] = value

    def DeleteValue(self, _key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]

    def CloseKey(self, _key):
        return None


class _FakeKernel32:
    def __init__(self, last_error=0, errors=None):
        self.last_error = last_error
        self.errors = list(errors or [])
        self.closed = []
        self.created = []

    def CreateMutexW(self, _attributes, _owner, name):
        self.created.append(name)
        if self.errors:
            self.last_error = self.errors[len(self.created) - 1]
        return 122 + len(self.created)

    def GetLastError(self):
        return self.last_error

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return True


class _FakeUser32:
    def __init__(self, hwnd=456, iconic=False):
        self.hwnd = hwnd
        self.iconic = iconic
        self.shown = []
        self.activated = []

    def FindWindowW(self, _class_name, _title):
        return self.hwnd

    def IsIconic(self, _hwnd):
        return self.iconic

    def ShowWindow(self, hwnd, command):
        self.shown.append((hwnd, command))
        return True

    def SetForegroundWindow(self, hwnd):
        self.activated.append(hwnd)
        return True


class WindowsDesktopTest(unittest.TestCase):
    def test_startup_command_quotes_frozen_executable(self):
        command = windows_desktop.startup_command(
            executable=r'C:\Program Files\CXVPNTools\CXVPNTools.exe',
            frozen=True)

        self.assertEqual(
            r'"C:\Program Files\CXVPNTools\CXVPNTools.exe" --startup',
            command)

    def test_source_startup_command_includes_main(self):
        command = windows_desktop.startup_command(
            executable=r'C:\Python\python.exe',
            app_base=r'C:\Project Folder\cxvpn', frozen=False)

        self.assertIn(r'"C:\Project Folder\cxvpn\main.py"', command)
        self.assertTrue(command.endswith('--startup'))

    def test_enable_disable_startup_uses_current_command(self):
        registry = _FakeRegistry()
        command = r'C:\App\CXVPNTools.exe --startup'

        windows_desktop.set_startup_enabled(
            True, registry=registry, command=command)
        self.assertTrue(windows_desktop.is_startup_enabled(
            registry=registry, command=command))
        self.assertFalse(windows_desktop.is_startup_enabled(
            registry=registry,
            command=r'D:\Moved\CXVPNTools.exe --startup'))

        windows_desktop.set_startup_enabled(False, registry=registry)
        self.assertFalse(windows_desktop.is_startup_enabled(
            registry=registry, command=command))

    def test_startup_brand_migration_removes_legacy_run_values(self):
        registry = _FakeRegistry()
        for name in windows_desktop.LEGACY_APP_NAMES:
            registry.values[name] = r'C:\Old\CX VPN TOOLS.exe --startup'

        windows_desktop.set_startup_enabled(
            True, registry=registry,
            command=r'C:\App\CXVPNTools.exe --startup')

        self.assertEqual(
            registry.values[windows_desktop.APP_NAME],
            r'C:\App\CXVPNTools.exe --startup')
        self.assertTrue(all(
            name not in registry.values
            for name in windows_desktop.LEGACY_APP_NAMES))

    def test_startup_brand_migration_preserves_enabled_state(self):
        registry = _FakeRegistry()
        registry.values['CX VPN TOOLS'] = (
            r'C:\Old\CX VPN TOOLS.exe --startup')

        migrated = windows_desktop.migrate_legacy_startup_registration(
            registry=registry,
            command=r'C:\App\CXVPNTools.exe --startup')

        self.assertTrue(migrated)
        self.assertEqual(
            r'C:\App\CXVPNTools.exe --startup',
            registry.values[windows_desktop.APP_NAME])
        self.assertNotIn('CX VPN TOOLS', registry.values)

    def test_startup_brand_migration_keeps_existing_current_value(self):
        registry = _FakeRegistry()
        registry.values[windows_desktop.APP_NAME] = 'current-command'
        registry.values['CXVPN管理器'] = 'legacy-command'

        windows_desktop.migrate_legacy_startup_registration(
            registry=registry, command='replacement-command')

        self.assertEqual(
            'current-command', registry.values[windows_desktop.APP_NAME])
        self.assertNotIn('CXVPN管理器', registry.values)

    def test_only_user_close_is_hidden_to_tray(self):
        self.assertTrue(windows_desktop.should_hide_to_tray('UserClosing'))
        self.assertFalse(windows_desktop.should_hide_to_tray('WindowsShutDown'))
        self.assertFalse(windows_desktop.should_hide_to_tray(
            'UserClosing', exiting=True))
        self.assertFalse(windows_desktop.should_hide_to_tray(
            'UserClosing', enabled=False))

    def test_single_instance_guard_holds_and_releases_mutex(self):
        kernel32 = _FakeKernel32()
        guard = windows_desktop.SingleInstanceGuard(kernel32=kernel32)

        self.assertTrue(guard.acquire())
        guard.close()

        self.assertEqual(
            [windows_desktop.INSTANCE_MUTEX,
             *windows_desktop.LEGACY_INSTANCE_MUTEXES],
            kernel32.created)
        self.assertEqual([124, 123], kernel32.closed)

    def test_single_instance_guard_rejects_duplicate(self):
        kernel32 = _FakeKernel32(windows_desktop.ERROR_ALREADY_EXISTS)
        guard = windows_desktop.SingleInstanceGuard(kernel32=kernel32)

        self.assertFalse(guard.acquire())
        self.assertEqual([123], kernel32.closed)

    def test_single_instance_guard_rejects_running_legacy_version(self):
        kernel32 = _FakeKernel32(errors=[
            0, windows_desktop.ERROR_ALREADY_EXISTS])
        guard = windows_desktop.SingleInstanceGuard(kernel32=kernel32)

        self.assertFalse(guard.acquire())
        self.assertEqual([124, 123], kernel32.closed)

    def test_existing_window_is_shown_and_activated(self):
        user32 = _FakeUser32(iconic=True)

        restored = windows_desktop.activate_existing_window(
            user32=user32, attempts=1, delay=0)

        self.assertTrue(restored)
        self.assertEqual([(456, 5), (456, 9)], user32.shown)
        self.assertEqual([456], user32.activated)

    def test_tray_left_click_shows_window_only_for_left_button(self):
        controller = windows_desktop.DesktopController(
            window=None, close_to_tray=lambda: True, on_exit=lambda: None)
        controller._mouse_buttons = SimpleNamespace(Left='left')
        controller.show_window = mock.Mock()

        controller._on_mouse_click(None, SimpleNamespace(Button='right'))
        controller._on_mouse_click(None, SimpleNamespace(Button='left'))

        controller.show_window.assert_called_once_with()

    def test_global_hotkey_map_uses_fixed_ctrl_alt_shortcuts(self):
        actions = windows_desktop.HOTKEY_ACTIONS

        self.assertEqual(actions[1], (
            'toggle_proxy', windows_desktop.MOD_CONTROL | windows_desktop.MOD_ALT,
            ord('P')))
        self.assertEqual(actions[2][0], 'toggle_mode')
        self.assertEqual(actions[3][0], 'open_connections')

    def test_open_page_restores_window_then_navigates(self):
        calls = []
        controller = windows_desktop.DesktopController(
            window=None, close_to_tray=lambda: True, on_exit=lambda: None,
            on_open_page=lambda page: calls.append(('open', page)))
        controller._show_native = lambda: calls.append(('show', None))
        controller._invoke = lambda action: action()

        controller.open_page('connections')

        self.assertEqual(calls, [
            ('show', None), ('open', 'connections')])

    def test_form_closing_handler_hides_instead_of_exiting(self):
        controller = windows_desktop.DesktopController(
            window=None, close_to_tray=lambda: True, on_exit=lambda: None)
        controller.hide_window = mock.Mock()
        args = SimpleNamespace(CloseReason='UserClosing', Cancel=False)

        controller._on_form_closing(None, args)

        self.assertTrue(args.Cancel)
        controller.hide_window.assert_called_once_with()

    def test_shutdown_reason_invokes_proxy_cleanup(self):
        cleanup = mock.Mock()
        controller = windows_desktop.DesktopController(
            window=None, close_to_tray=lambda: True, on_exit=lambda: None,
            on_os_shutdown=cleanup)
        args = SimpleNamespace(CloseReason='WindowsShutDown', Cancel=False)

        controller._on_form_closing(None, args)

        self.assertFalse(args.Cancel)
        cleanup.assert_called_once_with()

    def test_plain_user_close_never_invokes_proxy_cleanup(self):
        cleanup = mock.Mock()
        controller = windows_desktop.DesktopController(
            window=None, close_to_tray=lambda: False, on_exit=lambda: None,
            on_os_shutdown=cleanup)
        args = SimpleNamespace(CloseReason='UserClosing', Cancel=False)

        controller._on_form_closing(None, args)

        self.assertFalse(args.Cancel)
        cleanup.assert_not_called()

    def test_proxy_cleanup_failure_does_not_block_shutdown(self):
        def failing():
            raise RuntimeError('registry gone')
        controller = windows_desktop.DesktopController(
            window=None, close_to_tray=lambda: True, on_exit=lambda: None,
            on_os_shutdown=failing)
        args = SimpleNamespace(CloseReason='TaskManagerClosing', Cancel=False)

        controller._on_form_closing(None, args)

        self.assertFalse(args.Cancel)


if __name__ == '__main__':
    unittest.main()
