# -*- coding: utf-8 -*-
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from api import Api
from core import routing


class ApiDesktopActionTests(unittest.TestCase):
    def instance(self, config=None):
        target = Api.__new__(Api)
        target._routing_lock = threading.Lock()
        target._cfg_get = mock.Mock(return_value={
            'routing': config or routing.default_config(),
            'global_hotkeys_enabled': True,
            'lightweight_mode': True,
        })
        target._apply_routing_locked = mock.Mock(
            return_value={'ok': True, 'msg': '已应用'})
        target._desktop = SimpleNamespace(hotkeys_active=True)
        return target

    def test_tray_snapshot_uses_only_persisted_safe_nodes(self):
        config = routing.normalize_config({
            **routing.default_config(),
            'proxy_providers': [{
                'id': 'alpha', 'name': '订阅一',
                'url': 'https://example.test/sub', 'enabled': True,
                'selection_mode': 'manual', 'selected_node': '节点 B',
            }],
            'default_outbound': 'proxy:alpha',
        })
        target = self.instance(config)
        with mock.patch(
                'api.routing.subscription_store.load_node_snapshot',
                return_value={'nodes': [
                    {'name': '节点 A', 'alive': True, 'delay': 30},
                    {'name': '节点 B', 'alive': True, 'delay': 80},
                ]}):
            result = target.desktop_quick_snapshot()

        self.assertEqual(result['providers'][0]['nodes'][0], {
            'name': '节点 B', 'selected': True})
        self.assertNotIn('url', str(result))

    def test_tray_toggle_and_mode_use_atomic_apply_path(self):
        target = self.instance()

        target.desktop_toggle_routing()
        toggled = target._apply_routing_locked.call_args.args
        self.assertTrue(toggled[0]['enabled'])
        self.assertEqual(toggled[1], 'desktop_toggle')

        target.desktop_set_traffic_mode('global')
        mode = target._apply_routing_locked.call_args.args
        self.assertEqual(mode[0]['traffic_mode'], 'global')
        self.assertEqual(mode[1], 'desktop_mode')

    def test_tray_node_switch_uses_snapshot_and_atomic_apply(self):
        config = routing.normalize_config({
            **routing.default_config(),
            'proxy_providers': [{
                'id': 'alpha', 'name': '订阅一',
                'url': 'https://example.test/sub', 'enabled': True,
                'selection_mode': 'auto', 'selected_node': '',
            }],
            'default_outbound': 'proxy:alpha',
        })
        target = self.instance(config)
        with mock.patch(
                'api.routing.subscription_store.load_node_snapshot',
                return_value={'nodes': [{'name': '节点 B'}]}):
            result = target.desktop_select_node('alpha', '节点 B')

        self.assertTrue(result['ok'])
        applied, source = target._apply_routing_locked.call_args.args
        self.assertEqual(source, 'desktop_node')
        self.assertEqual(applied['proxy_providers'][0]['selection_mode'], 'manual')
        self.assertEqual(applied['proxy_providers'][0]['selected_node'], '节点 B')

    @mock.patch('api.windows_desktop.is_startup_enabled', return_value=True)
    def test_desktop_settings_report_requested_and_active_modes(self, _startup):
        target = self.instance()

        result = target.get_desktop_settings()

        self.assertTrue(result['startup_enabled'])
        self.assertTrue(result['global_hotkeys_enabled'])
        self.assertTrue(result['global_hotkeys_active'])
        self.assertTrue(result['lightweight_mode'])


if __name__ == '__main__':
    unittest.main()
