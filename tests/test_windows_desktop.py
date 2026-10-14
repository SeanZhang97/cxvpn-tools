# -*- coding: utf-8 -*-
import threading
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


class _FakeMenuEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class _FakeMenuItems:
    def __init__(self):
        self.items = []

    def Add(self, item):
        self.items.append(item)

    def Clear(self):
        self.items.clear()

    def __iter__(self):
        return iter(self.items)

    def __len__(self):
        return len(self.items)


class _FakeMenuItem:
    def __init__(self, text=''):
        self.Text = text
        self.Tag = None
        self.Checked = False
        self.Enabled = True
        self.MaxDropDownItems = None
        self.DropDownItems = _FakeMenuItems()
        self.Click = _FakeMenuEvent()


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

    def test_startup_registration_repairs_existing_stale_current_value(self):
        registry = _FakeRegistry()
        registry.values[windows_desktop.APP_NAME] = 'current-command'
        registry.values['CXVPN管理器'] = 'legacy-command'

        windows_desktop.migrate_legacy_startup_registration(
            registry=registry, command='replacement-command')

        self.assertEqual(
            'replacement-command', registry.values[windows_desktop.APP_NAME])
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

    def test_dispose_cleans_tray_when_native_already_disposed(self):
        calls = []
        notify = SimpleNamespace(
            Visible=True, Dispose=lambda: calls.append('notify.dispose'))
        menu = SimpleNamespace(
            Dispose=lambda: calls.append('menu.dispose'))
        icon = SimpleNamespace(
            Dispose=lambda: calls.append('icon.dispose'))
        controller = windows_desktop.DesktopController(
            window=None, close_to_tray=lambda: True, on_exit=lambda: None)
        controller._notify = notify
        controller._menu = menu
        controller._icon = icon
        native = mock.Mock()
        native.IsDisposed = True
        controller._native = native

        controller.dispose()

        self.assertIn('notify.dispose', calls)
        self.assertIn('menu.dispose', calls)
        self.assertIn('icon.dispose', calls)
        self.assertIsNone(controller._notify)
        self.assertIsNone(controller._menu)
        self.assertIsNone(controller._icon)

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

    def test_prepare_update_exit_suppresses_close_to_tray(self):
        controller = windows_desktop.DesktopController(
            window=None, close_to_tray=lambda: True, on_exit=lambda: None)
        controller.hide_window = mock.Mock()
        before = SimpleNamespace(CloseReason='UserClosing', Cancel=False)
        controller._on_form_closing(None, before)
        self.assertTrue(before.Cancel)
        controller.hide_window.assert_called_once()

        controller.prepare_update_exit()
        during = SimpleNamespace(CloseReason='UserClosing', Cancel=False)
        controller._on_form_closing(None, during)

        self.assertFalse(during.Cancel)
        self.assertEqual(controller.hide_window.call_count, 1)

    def test_restore_from_update_exit_reenables_close_to_tray(self):
        controller = windows_desktop.DesktopController(
            window=None, close_to_tray=lambda: True, on_exit=lambda: None)
        controller.hide_window = mock.Mock()
        controller.prepare_update_exit()
        controller.restore_from_update_exit()

        args = SimpleNamespace(CloseReason='UserClosing', Cancel=False)
        controller._on_form_closing(None, args)

        self.assertTrue(args.Cancel)
        controller.hide_window.assert_called_once()

    def test_prepare_update_exit_hides_and_restore_shows_notify_icon(self):
        controller = windows_desktop.DesktopController(
            window=None, close_to_tray=lambda: False, on_exit=lambda: None)
        controller._native = SimpleNamespace(
            IsDisposed=False, InvokeRequired=False)
        controller._action_type = lambda action: action
        notify = mock.Mock()
        controller._notify = notify

        controller.prepare_update_exit()
        self.assertIs(notify.Visible, False)
        controller.restore_from_update_exit()
        self.assertIs(notify.Visible, True)

    @staticmethod
    def _controller_with_inline_ui_thread(on_open_page):
        controller = windows_desktop.DesktopController(
            window=None, close_to_tray=lambda: False, on_exit=lambda: None,
            on_open_page=on_open_page)
        controller._native = SimpleNamespace(
            IsDisposed=False, InvokeRequired=False, WindowState='normal',
            Show=mock.Mock(), Activate=mock.Mock())
        controller._window_state = SimpleNamespace(Minimized='min')
        controller._action_type = lambda action: action
        return controller

    def test_open_page_navigates_off_the_ui_thread(self):
        pages = []
        controller = self._controller_with_inline_ui_thread(pages.append)

        with mock.patch('core.windows_desktop.threading.Thread') as thread_cls:
            thread_cls.return_value = mock.Mock()
            controller.open_page('connections')

        # _invoke 在 UI 线程内联执行：窗口已显示，但导航不得在 UI 线程运行。
        controller._native.Show.assert_called_once()
        self.assertEqual(pages, [])
        thread_cls.assert_called_once()
        kwargs = thread_cls.call_args.kwargs
        self.assertEqual(kwargs.get('name'), 'tray-open-page')
        kwargs['target'](*kwargs['args'])
        self.assertEqual(pages, ['connections'])

    def test_open_page_navigation_failure_is_logged_not_raised(self):
        def failing(_page):
            raise RuntimeError('bridge gone')
        controller = self._controller_with_inline_ui_thread(failing)
        controller.log = mock.Mock()

        with mock.patch('core.windows_desktop.threading.Thread') as thread_cls:
            thread_cls.return_value = mock.Mock()
            controller.open_page('overview')
            kwargs = thread_cls.call_args.kwargs

        kwargs['target'](*kwargs['args'])
        self.assertTrue(any(
            '页面快捷入口失败' in call.args[0]
            for call in controller.log.call_args_list))

    def _controller_for_menu(self, snapshot):
        controller = windows_desktop.DesktopController(
            window=None, close_to_tray=lambda: False, on_exit=lambda: None,
            quick_snapshot=lambda: snapshot)
        controller._menu_item_type = _FakeMenuItem
        controller._tray_items = {
            'status': _FakeMenuItem(), 'toggle': _FakeMenuItem(),
            'rule': _FakeMenuItem(), 'global': _FakeMenuItem(),
            'nodes': _FakeMenuItem(),
        }
        return controller

    def test_menu_opening_groups_regions_with_status_suffix(self):
        snapshot = {
            'enabled': True, 'traffic_mode': 'rule',
            'providers': [{
                'id': 'p1', 'name': '订阅一',
                'groups': [
                    {'code': 'HK', 'label': '香港', 'nodes': [
                        {'name': '香港 01', 'selected': True,
                         'alive': True, 'delay': 66},
                        {'name': '香港 02', 'alive': False, 'delay': 0},
                        {'name': '香港 03'},
                    ]},
                    {'code': 'US', 'label': '美国', 'nodes': [
                        {'name': '美国 01', 'alive': True, 'delay': 245},
                    ]},
                ],
            }],
        }
        controller = self._controller_for_menu(snapshot)

        controller._on_menu_opening(None, None)

        limit = windows_desktop.TRAY_MAX_DROPDOWN_ITEMS
        nodes_menu = controller._tray_items['nodes']
        self.assertTrue(nodes_menu.Enabled)
        self.assertEqual(nodes_menu.MaxDropDownItems, limit)
        [provider_menu] = nodes_menu.DropDownItems.items
        self.assertEqual(provider_menu.Text, '订阅一')
        self.assertEqual(provider_menu.MaxDropDownItems, limit)
        region_items = provider_menu.DropDownItems.items
        self.assertEqual([item.Text for item in region_items],
                         ['香港 (3)', '美国 (1)'])
        for region_item in region_items:
            self.assertEqual(region_item.MaxDropDownItems, limit)
        self.assertEqual(
            [item.Text for item in region_items[0].DropDownItems],
            ['香港 01 · 66ms', '香港 02 · 不可用', '香港 03'])
        first = region_items[0].DropDownItems.items[0]
        self.assertEqual(first.Tag, 'p1\0香港 01')
        self.assertTrue(first.Checked)
        second = region_items[0].DropDownItems.items[1]
        self.assertEqual(second.Tag, 'p1\0香港 02')
        self.assertFalse(second.Checked)
        self.assertEqual(
            [item.Text for item in region_items[1].DropDownItems],
            ['美国 01 · 245ms'])

    def test_menu_opening_single_region_stays_flat_and_scrollable(self):
        snapshot = {
            'enabled': False, 'traffic_mode': 'global',
            'providers': [{
                'id': 'p1', 'name': '订阅一',
                'groups': [{'code': 'OTHER', 'label': '其他', 'nodes': [
                    {'name': f'节点 {i}'} for i in range(30)]}],
            }],
        }
        controller = self._controller_for_menu(snapshot)

        controller._on_menu_opening(None, None)

        nodes_menu = controller._tray_items['nodes']
        self.assertTrue(nodes_menu.Enabled)
        self.assertEqual(nodes_menu.MaxDropDownItems,
                         windows_desktop.TRAY_MAX_DROPDOWN_ITEMS)
        [provider_menu] = nodes_menu.DropDownItems.items
        self.assertEqual(provider_menu.MaxDropDownItems,
                         windows_desktop.TRAY_MAX_DROPDOWN_ITEMS)
        items = provider_menu.DropDownItems.items
        self.assertEqual(len(items), 30)
        self.assertEqual(items[0].Text, '节点 0')
        self.assertEqual(items[0].Tag, 'p1\0节点 0')

    def test_request_exit_cleans_tray_then_runs_on_exit(self):
        exited = threading.Event()
        controller = windows_desktop.DesktopController(
            window=None, close_to_tray=lambda: True,
            on_exit=exited.set)
        controller._native = SimpleNamespace(
            IsDisposed=False, InvokeRequired=False)
        controller._action_type = lambda action: action
        notify = mock.Mock()
        controller._notify = notify

        controller.request_exit()

        self.assertTrue(controller._exiting)
        self.assertIs(notify.Visible, False)
        notify.Dispose.assert_called_once()
        self.assertTrue(exited.wait(2))

    def test_request_exit_is_idempotent(self):
        calls = []
        controller = windows_desktop.DesktopController(
            window=None, close_to_tray=lambda: False, on_exit=lambda: calls.append(1))
        controller._native = SimpleNamespace(
            IsDisposed=False, InvokeRequired=False)
        controller._action_type = lambda action: action
        controller._notify = mock.Mock()

        controller.request_exit()
        controller.request_exit()

        self.assertTrue(controller._exiting)
        self.assertEqual(len(calls), 1)

    def test_shutdown_watcher_triggers_request_exit_on_signal(self):
        controller = windows_desktop.DesktopController(
            window=None, close_to_tray=lambda: False, on_exit=lambda: None)
        controller.request_exit = mock.Mock()
        event = mock.Mock()
        event.WaitOne.side_effect = [False, True]
        controller._shutdown_event = event

        controller._watch_shutdown_event()

        controller.request_exit.assert_called_once_with()

    def test_shutdown_watcher_stops_when_already_exiting(self):
        controller = windows_desktop.DesktopController(
            window=None, close_to_tray=lambda: False, on_exit=lambda: None)
        controller.request_exit = mock.Mock()
        controller._exiting = True
        event = mock.Mock()
        controller._shutdown_event = event

        controller._watch_shutdown_event()

        event.WaitOne.assert_not_called()
        controller.request_exit.assert_not_called()

    def test_dispose_closes_shutdown_event(self):
        controller = windows_desktop.DesktopController(
            window=None, close_to_tray=lambda: False, on_exit=lambda: None)
        controller._native = SimpleNamespace(
            IsDisposed=True, InvokeRequired=False)
        controller._action_type = lambda action: action
        event = mock.Mock()
        controller._shutdown_event = event

        controller.dispose()

        event.Close.assert_called_once()
        self.assertIsNone(controller._shutdown_event)


if __name__ == '__main__':
    unittest.main()
