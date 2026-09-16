# -*- coding: utf-8 -*-
"""WinINet 无效手动代理修复：注册表和 Windows API 全部使用替身。"""
import ctypes
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from core import proxy_guard
from tests.test_proxy_guard import _FakeRegistry


class InvalidProxyRepairTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.registry = _FakeRegistry()
        self.registry.values.update(ProxyEnable=1, ProxyServer=':',
                                    ProxyOverride='内网 e\u0301 🇨🇳', AutoConfigURL='https://example.test/pac')
        self.flags = 15
        self.wininet = mock.Mock()
        self.wininet.InternetQueryOptionW.side_effect = self.query
        self.wininet.InternetSetOptionW.side_effect = self.write

    def query(self, handle, code, pointer, size):
        settings = pointer._obj
        self.assertIsNone(handle)
        self.assertEqual(75, code)
        self.assertIsNone(settings.connection)
        self.assertEqual(1, settings.count)
        self.assertEqual(10, settings.options[0].option)
        settings.options[0].value.number = self.flags
        return 1

    def write(self, handle, code, pointer, size):
        settings = pointer._obj
        self.assertIsNone(handle)
        self.assertEqual(75, code)
        self.assertIsNone(settings.connection)
        self.assertEqual(2, settings.count)
        self.assertEqual([1, 2], [settings.options[i].option for i in range(2)])
        self.assertEqual('', ctypes.wstring_at(settings.options[1].value.string))
        self.flags = int(settings.options[0].value.number)
        self.registry.values.update(ProxyEnable=0, ProxyServer='')
        return 1

    def repair(self):
        return proxy_guard.repair_invalid_manual_proxy(
            registry=self.registry, wininet=self.wininet, base=self.root.name)

    def test_repairs_colon_preserving_pac_bypass_and_auto_detect(self):
        self.assertTrue(self.repair())
        self.assertEqual(13, self.flags)
        self.assertEqual('内网 e\u0301 🇨🇳', self.registry.values['ProxyOverride'])
        self.assertEqual('https://example.test/pac', self.registry.values['AutoConfigURL'])
        backup = next(Path(self.root.name).glob('proxy-invalid-*.json'))
        saved = json.loads(backup.read_text(encoding='utf-8'))
        self.assertEqual(':', saved['state']['server'])
        self.assertEqual('内网 e\u0301 🇨🇳', saved['state']['override'])
        self.assertEqual(15, saved['wininet_flags'])

    def test_repairs_enabled_empty_endpoint(self):
        self.registry.values['ProxyServer'] = ''
        self.assertTrue(self.repair())
        self.assertEqual(0, self.registry.values['ProxyEnable'])

    def test_legal_or_disabled_endpoints_are_never_changed(self):
        for enabled, server in [(1, '127.0.0.1:17890'), (1, 'proxy.example:8080'),
                                (1, 'http=proxy:8080;https=proxy:8443'),
                                (1, '[::1]:7897'), (0, ':'), (0, '')]:
            with self.subTest(enabled=enabled, server=server):
                self.registry.values.update(ProxyEnable=enabled, ProxyServer=server)
                self.assertFalse(self.repair())
        self.wininet.InternetSetOptionW.assert_not_called()
        self.wininet.InternetQueryOptionW.assert_not_called()

    def test_backup_failure_prevents_mutation(self):
        with mock.patch.object(proxy_guard, '_atomic_json', side_effect=OSError('backup denied')):
            with self.assertRaises(OSError):
                self.repair()
        self.wininet.InternetSetOptionW.assert_not_called()

    def test_external_proxy_change_during_backup_is_preserved(self):
        def concurrent_change(*args):
            self.registry.values['ProxyServer'] = 'proxy.example:8080'
        with mock.patch.object(proxy_guard, '_atomic_json', side_effect=concurrent_change):
            with self.assertRaisesRegex(OSError, '设置已变化'):
                self.repair()
        self.wininet.InternetSetOptionW.assert_not_called()

    def test_failed_readback_is_not_success(self):
        self.wininet.InternetSetOptionW.side_effect = lambda *args: 1
        with self.assertRaisesRegex(OSError, '回读不一致'):
            self.repair()

    def test_registry_read_failure_cannot_masquerade_as_disabled(self):
        with mock.patch.object(self.registry, 'OpenKey', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                self.repair()
        self.wininet.InternetSetOptionW.assert_not_called()


if __name__ == '__main__':
    unittest.main()
