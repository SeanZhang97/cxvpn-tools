# -*- coding: utf-8 -*-
import json
import os
import tempfile
import unittest

from core import proxy_guard


class _FakeRegistry:
    HKEY_CURRENT_USER = object()
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_DWORD = 4
    REG_SZ = 1

    def __init__(self):
        self.values = {}

    def OpenKey(self, _root, _path, _reserved, _access):
        return object()

    def QueryValueEx(self, _key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        value = self.values[name]
        kind = self.REG_DWORD if isinstance(value, int) else self.REG_SZ
        return value, kind

    def SetValueEx(self, _key, name, _reserved, _kind, value):
        self.values[name] = value

    def CloseKey(self, _key):
        return None


def _proxy_set(registry, enabled=True, server='127.0.0.1:7897'):
    registry.values['ProxyEnable'] = 1 if enabled else 0
    if server:
        registry.values['ProxyServer'] = server


_dead_port = lambda _host, _port: False
_alive_port = lambda _host, _port: True
_silent_log = lambda _msg: None


class ProxyGuardTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = self._tmp.name
        self.registry = _FakeRegistry()

    def tearDown(self):
        self._tmp.cleanup()

    def _snapshot_path(self):
        return os.path.join(self.base, proxy_guard.SNAPSHOT_FILE_NAME)

    def _read_snapshot(self):
        with open(self._snapshot_path(), encoding='utf-8') as f:
            return json.load(f)

    # ---------- 脏标记 ----------
    def test_marker_lifecycle(self):
        self.assertFalse(proxy_guard.is_last_run_dirty(self.base))
        proxy_guard.mark_running(self.base)
        self.assertTrue(proxy_guard.is_last_run_dirty(self.base))
        proxy_guard.mark_clean(self.base)
        self.assertFalse(proxy_guard.is_last_run_dirty(self.base))

    # ---------- 启动自检 ----------
    def test_startup_without_marker_does_not_repair(self):
        _proxy_set(self.registry)

        repaired = proxy_guard.startup_check(
            log=print, registry=self.registry, base=self.base,
            port_check=_dead_port)

        self.assertIsNone(repaired)
        self.assertEqual(1, self.registry.values['ProxyEnable'])
        self.assertFalse(os.path.exists(self._snapshot_path()))
        self.assertTrue(proxy_guard.is_last_run_dirty(self.base))

    def test_startup_with_marker_and_dead_port_repairs(self):
        proxy_guard.mark_running(self.base)
        _proxy_set(self.registry)
        logs = []

        repaired = proxy_guard.startup_check(
            log=logs.append, registry=self.registry, base=self.base,
            port_check=_dead_port)

        self.assertEqual('127.0.0.1:7897', repaired)
        self.assertEqual(0, self.registry.values['ProxyEnable'])
        self.assertEqual(1, len(logs))
        self.assertIn('残留', logs[0])
        snapshot = self._read_snapshot()
        self.assertEqual('startup_repair', snapshot['reason'])
        self.assertEqual('127.0.0.1:7897', snapshot['proxy_server'])
        self.assertTrue(snapshot['proxy_enable'])
        # 修复后进入新一轮存活期
        self.assertTrue(proxy_guard.is_last_run_dirty(self.base))

    def test_startup_with_marker_and_listening_port_keeps_proxy(self):
        proxy_guard.mark_running(self.base)
        _proxy_set(self.registry)

        repaired = proxy_guard.startup_check(
            log=print, registry=self.registry, base=self.base,
            port_check=_alive_port)

        self.assertIsNone(repaired)
        self.assertEqual(1, self.registry.values['ProxyEnable'])
        self.assertFalse(os.path.exists(self._snapshot_path()))

    def test_startup_with_marker_but_proxy_disabled_no_op(self):
        proxy_guard.mark_running(self.base)
        _proxy_set(self.registry, enabled=False)

        repaired = proxy_guard.startup_check(
            log=print, registry=self.registry, base=self.base,
            port_check=_dead_port)

        self.assertIsNone(repaired)
        self.assertFalse(os.path.exists(self._snapshot_path()))

    def test_startup_with_marker_foreign_proxy_untouched(self):
        proxy_guard.mark_running(self.base)
        _proxy_set(self.registry, server='proxy.corp.example.com:8080')

        repaired = proxy_guard.startup_check(
            log=print, registry=self.registry, base=self.base,
            port_check=_dead_port)

        self.assertIsNone(repaired)
        self.assertEqual(1, self.registry.values['ProxyEnable'])
        self.assertFalse(os.path.exists(self._snapshot_path()))

    def test_startup_with_marker_mixed_entries_untouched(self):
        proxy_guard.mark_running(self.base)
        _proxy_set(
            self.registry,
            server='http=127.0.0.1:7897;https=proxy.corp.example.com:8080')

        repaired = proxy_guard.startup_check(
            log=print, registry=self.registry, base=self.base,
            port_check=_dead_port)

        self.assertIsNone(repaired)
        self.assertEqual(1, self.registry.values['ProxyEnable'])

    # ---------- 关机清扫 ----------
    def test_shutdown_clears_loopback_proxy_regardless_of_port(self):
        proxy_guard.mark_running(self.base)
        _proxy_set(self.registry)

        cleared = proxy_guard.shutdown_cleanup(
            log=_silent_log, registry=self.registry, base=self.base)

        self.assertEqual('127.0.0.1:7897', cleared)
        self.assertEqual(0, self.registry.values['ProxyEnable'])
        snapshot = self._read_snapshot()
        self.assertEqual('shutdown_cleanup', snapshot['reason'])
        self.assertFalse(proxy_guard.is_last_run_dirty(self.base))

    def test_shutdown_foreign_proxy_untouched_but_marker_cleared(self):
        proxy_guard.mark_running(self.base)
        _proxy_set(self.registry, server='proxy.corp.example.com:8080')

        cleared = proxy_guard.shutdown_cleanup(
            log=_silent_log, registry=self.registry, base=self.base)

        self.assertIsNone(cleared)
        self.assertEqual(1, self.registry.values['ProxyEnable'])
        self.assertFalse(os.path.exists(self._snapshot_path()))
        self.assertFalse(proxy_guard.is_last_run_dirty(self.base))

    def test_shutdown_disabled_proxy_untouched_but_marker_cleared(self):
        proxy_guard.mark_running(self.base)
        _proxy_set(self.registry, enabled=False)

        cleared = proxy_guard.shutdown_cleanup(
            log=_silent_log, registry=self.registry, base=self.base)

        self.assertIsNone(cleared)
        self.assertFalse(os.path.exists(self._snapshot_path()))
        self.assertFalse(proxy_guard.is_last_run_dirty(self.base))

    def test_normal_exit_then_next_startup_no_repair(self):
        _proxy_set(self.registry)
        proxy_guard.shutdown_cleanup(
            log=_silent_log, registry=self.registry, base=self.base)
        self.assertEqual(0, self.registry.values['ProxyEnable'])
        # 外部工具随后重新开启代理并正常存活
        _proxy_set(self.registry)

        repaired = proxy_guard.startup_check(
            log=print, registry=self.registry, base=self.base,
            port_check=_dead_port)

        self.assertIsNone(repaired)
        self.assertEqual(1, self.registry.values['ProxyEnable'])

    # ---------- 解析 ----------
    def test_entries_parse_plain_and_schemed_and_equated_forms(self):
        loopback, foreign = proxy_guard.loopback_entries(
            'localhost:1080;https=http://127.0.0.1:7897')
        self.assertEqual([('localhost', 1080), ('127.0.0.1', 7897)], loopback)
        self.assertFalse(foreign)

    def test_entries_detect_foreign(self):
        loopback, foreign = proxy_guard.loopback_entries('10.0.0.8:3128')
        self.assertEqual([], loopback)
        self.assertTrue(foreign)

    # ---------- 注册表读写 ----------
    def test_set_proxy_enabled_writes_dword(self):
        proxy_guard.set_proxy_enabled(False, registry=self.registry)
        self.assertEqual(0, self.registry.values['ProxyEnable'])
        proxy_guard.set_proxy_enabled(True, registry=self.registry)
        self.assertEqual(1, self.registry.values['ProxyEnable'])

    def test_read_proxy_state_defaults_when_keys_missing(self):
        state = proxy_guard.read_proxy_state(registry=self.registry)
        self.assertFalse(state['enable'])
        self.assertEqual('', state['server'])
        self.assertEqual('', state['override'])
        self.assertEqual('', state['auto_config'])


if __name__ == '__main__':
    unittest.main()