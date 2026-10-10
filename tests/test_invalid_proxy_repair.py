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
        self.persist_proxy = True
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
        self.assertIsNone(handle)
        if code in (39, 37):
            self.assertIsNone(pointer)
            self.assertEqual(0, size)
            return 1
        settings = pointer._obj
        self.assertEqual(75, code)
        self.assertIsNone(settings.connection)
        self.assertEqual(4, settings.count)
        self.assertEqual([1, 2, 3, 4], [settings.options[i].option for i in range(4)])
        self.assertEqual('', ctypes.wstring_at(settings.options[1].value.string))
        self.assertEqual(self.registry.values['ProxyOverride'],
                         ctypes.wstring_at(settings.options[2].value.string))
        self.assertEqual(self.registry.values['AutoConfigURL'],
                         ctypes.wstring_at(settings.options[3].value.string))
        self.flags = int(settings.options[0].value.number)
        if self.persist_proxy:
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

    def test_direct_flags_with_enabled_colon_residue_are_repaired_once(self):
        self.flags = 1
        self.persist_proxy = False
        with mock.patch.object(self.registry, 'SetValueEx', wraps=self.registry.SetValueEx) as write:
            self.assertTrue(self.repair())
        self.assertEqual([('ProxyEnable', 0), ('ProxyServer', '')],
                         [(call.args[1], call.args[4]) for call in write.call_args_list])
        self.assertEqual(1, self.flags)
        self.assertEqual('内网 e\u0301 🇨🇳', self.registry.values['ProxyOverride'])
        self.assertEqual('https://example.test/pac', self.registry.values['AutoConfigURL'])
        self.assertEqual([75, 39, 37, 39, 37],
                         [call.args[1] for call in self.wininet.InternetSetOptionW.call_args_list])
        backup = next(Path(self.root.name).glob('proxy-invalid-*.json'))
        saved = json.loads(backup.read_text(encoding='utf-8'))
        self.assertEqual(1, saved['wininet_flags'])
        self.assertEqual(':', saved['state']['server'])

    def test_disabled_colon_left_by_wininet_is_also_cleared(self):
        def leave_colon(*args):
            result = self.write(*args)
            if args[1] == 75:
                self.registry.values['ProxyServer'] = ':'
            return result
        self.wininet.InternetSetOptionW.side_effect = leave_colon
        self.assertTrue(self.repair())
        self.assertEqual('', self.registry.values['ProxyServer'])

    def test_proxy_only_flags_transition_to_direct(self):
        self.flags = 2
        self.assertTrue(self.repair())
        self.assertEqual(1, self.flags)

    def test_readback_refreshes_wininet_before_querying_new_flags(self):
        def delayed_write(handle, code, pointer, size):
            if code == 75:
                self.pending_flags = int(pointer._obj.options[0].value.number)
            elif code == 37:
                self.flags = self.pending_flags
                self.registry.values.update(ProxyEnable=0, ProxyServer='')
            return 1
        self.wininet.InternetSetOptionW.side_effect = delayed_write
        self.assertTrue(self.repair())
        self.assertEqual(13, self.flags)

    def test_refresh_failure_aborts_before_registry_fallback(self):
        self.flags = 1
        self.persist_proxy = False
        def failed_refresh(*args):
            return 0 if args[1] == 37 else self.write(*args)
        self.wininet.InternetSetOptionW.side_effect = failed_refresh
        with mock.patch.object(self.registry, 'SetValueEx') as write:
            with self.assertRaisesRegex(OSError, '刷新失败.*WinINetRefresh=False'):
                self.repair()
        write.assert_not_called()

    def test_third_party_endpoint_after_wininet_write_is_not_cleared(self):
        def takeover(*args):
            result = self.write(*args)
            if args[1] == 75:
                self.registry.values.update(ProxyEnable=1,
                                            ProxyServer='user:password@proxy.example:8080')
            return result
        self.wininet.InternetSetOptionW.side_effect = takeover
        with mock.patch.object(self.registry, 'SetValueEx') as write:
            with self.assertRaisesRegex(OSError, '回读不一致') as caught:
                self.repair()
        write.assert_not_called()
        self.assertNotIn('password', str(caught.exception))
        self.assertIn('ProxyServer=非空(隐藏)', str(caught.exception))

    def test_preserved_field_changes_are_reported_without_values(self):
        def change_pac(*args):
            result = self.write(*args)
            if args[1] == 75:
                self.registry.values['AutoConfigURL'] = 'https://private.test/pac?token=secret'
            return result
        self.wininet.InternetSetOptionW.side_effect = change_pac
        with mock.patch.object(self.registry, 'SetValueEx') as write:
            with self.assertRaisesRegex(OSError, 'AutoConfigURLChanged=True') as caught:
                self.repair()
        write.assert_not_called()
        self.assertNotIn('secret', str(caught.exception))
        self.assertNotIn('private.test', str(caught.exception))

    def test_takeover_before_fallback_registry_write_is_preserved(self):
        self.flags = 1
        self.persist_proxy = False
        original_open = self.registry.OpenKey
        def takeover(root, path, reserved, access):
            if access == self.registry.KEY_READ | self.registry.KEY_SET_VALUE:
                self.registry.values.update(ProxyEnable=1, ProxyServer='proxy.example:8080')
            return original_open(root, path, reserved, access)
        with mock.patch.object(self.registry, 'OpenKey', side_effect=takeover), \
                mock.patch.object(self.registry, 'SetValueEx') as write:
            with self.assertRaisesRegex(OSError, '设置已变化'):
                self.repair()
        write.assert_not_called()
        self.assertEqual('proxy.example:8080', self.registry.values['ProxyServer'])

    def test_takeover_between_residue_writes_preserves_new_endpoint(self):
        self.flags = 1
        self.persist_proxy = False
        original_write = self.registry.SetValueEx
        def takeover(*args):
            original_write(*args)
            if args[1] == 'ProxyEnable':
                self.registry.values.update(ProxyEnable=1, ProxyServer='proxy.example:8080')
        with mock.patch.object(self.registry, 'SetValueEx', side_effect=takeover) as write:
            with self.assertRaisesRegex(OSError, '未清空服务器残留'):
                self.repair()
        self.assertEqual(1, write.call_count)
        self.assertEqual('proxy.example:8080', self.registry.values['ProxyServer'])

    def test_flags_change_before_registry_fallback_prevents_write(self):
        self.flags = 1
        self.persist_proxy = False
        original_open = self.registry.OpenKey
        def flags_change(root, path, reserved, access):
            if access == self.registry.KEY_READ | self.registry.KEY_SET_VALUE:
                self.flags = 9
            return original_open(root, path, reserved, access)
        with mock.patch.object(self.registry, 'OpenKey', side_effect=flags_change), \
                mock.patch.object(self.registry, 'SetValueEx') as write:
            with self.assertRaisesRegex(OSError, '设置已变化'):
                self.repair()
        write.assert_not_called()

    def test_registry_fallback_permission_failure_is_not_success(self):
        self.flags = 1
        self.persist_proxy = False
        with mock.patch.object(self.registry, 'SetValueEx', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                self.repair()

    def test_residue_write_failure_is_bounded_and_not_success(self):
        self.flags = 1
        self.persist_proxy = False
        original_write = self.registry.SetValueEx
        def leave_server(*args):
            if args[1] != 'ProxyServer':
                original_write(*args)
        with mock.patch.object(self.registry, 'SetValueEx', side_effect=leave_server) as write:
            with self.assertRaisesRegex(OSError, '回读不一致.*ProxyServer=冒号'):
                self.repair()
        self.assertEqual(2, write.call_count)

    def test_unicode_diagnostic_output_failure_does_not_abort_repair(self):
        self.flags = 1
        self.persist_proxy = False
        log = mock.Mock(side_effect=UnicodeEncodeError('gbk', '🇨🇳', 0, 2, 'unsupported'))
        self.assertTrue(proxy_guard.repair_invalid_manual_proxy(
            registry=self.registry, wininet=self.wininet, base=self.root.name, log=log))
        self.assertEqual(0, self.registry.values['ProxyEnable'])
        self.assertEqual('内网 e\u0301 🇨🇳', self.registry.values['ProxyOverride'])


if __name__ == '__main__':
    unittest.main()
