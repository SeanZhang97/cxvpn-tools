# -*- coding: utf-8 -*-
import os
import tempfile
import tomllib
import unittest
from unittest import mock

from core import codex_proxy


class CodexProxyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cxvpn-codex-')
        self.root = self.temp.name
        self.codex_home = os.path.join(self.root, 'codex')
        self.data_root = os.path.join(self.root, 'user-data')
        self.environ = {
            'CODEX_HOME': self.codex_home,
            'USERPROFILE': os.path.join(self.root, 'profile'),
        }

    def tearDown(self):
        self.temp.cleanup()

    @property
    def config_path(self):
        return os.path.join(self.codex_home, 'config.toml')

    @property
    def snapshot_path(self):
        return os.path.join(
            self.data_root, codex_proxy.SNAPSHOT_FILE_NAME)

    def write_config(self, text):
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        with open(self.config_path, 'w', encoding='utf-8') as stream:
            stream.write(text)

    def read_config(self):
        with open(self.config_path, 'r', encoding='utf-8') as stream:
            return stream.read()

    def sync(self, port=17890):
        return codex_proxy.sync(
            port, environ=self.environ, data_root=self.data_root)

    def restore(self):
        return codex_proxy.restore(
            environ=self.environ, data_root=self.data_root)

    def test_missing_config_creates_target_sections(self):
        result = self.sync()

        self.assertTrue(result['ok'])
        parsed = self.parse_config()
        self.assertIs(True, parsed['features']['respect_system_proxy'])
        self.assertEqual(
            parsed['mcp_servers']['node_repl']['env']['HTTP_PROXY'],
            'http://127.0.0.1:17890')
        self.assertEqual(
            parsed['shell_environment_policy']['set']['HTTPS_PROXY'],
            'http://127.0.0.1:17890')
        self.assertEqual(
            parsed['shell_environment_policy']['set']['NO_PROXY'],
            parsed['mcp_servers']['node_repl']['env']['NO_PROXY'])
        self.assertEqual(
            parsed['shell_environment_policy']['set']['no_proxy'],
            parsed['mcp_servers']['node_repl']['env']['no_proxy'])
        self.assertTrue(os.path.isfile(self.snapshot_path))

    def test_existing_config_only_updates_target_fields(self):
        original = (
            '# 保留注释\n'
            '[features]\n'
            'respect_system_proxy = false # 原值\n'
            'other_feature = "保留"\n\n'
            '[mcp_servers.other.env]\n'
            'TOKEN = "不会被读取"\n\n'
            '[mcp_servers.node_repl.env]\n'
            'HTTP_PROXY = "http://old.example:1"\n'
            'CUSTOM = "保留"\n')
        self.write_config(original)

        self.assertTrue(self.sync()['ok'])
        text = self.read_config()
        parsed = self.parse_config()
        self.assertIn('# 保留注释', text)
        self.assertIn('other_feature = "保留"', text)
        self.assertIn('TOKEN = "不会被读取"', text)
        self.assertIn('CUSTOM = "保留"', text)
        self.assertIs(True, parsed['features']['respect_system_proxy'])
        self.assertEqual(
            parsed['mcp_servers']['node_repl']['env']['HTTP_PROXY'],
            'http://127.0.0.1:17890')

    def test_missing_sections_are_added(self):
        self.write_config('[other]\nvalue = "中文"\n')

        self.assertTrue(self.sync(18080)['ok'])
        parsed = self.parse_config()
        self.assertEqual(parsed['other']['value'], '中文')
        self.assertEqual(
            parsed['mcp_servers']['node_repl']['env']['ALL_PROXY'],
            'http://127.0.0.1:18080')
        self.assertIn('[shell_environment_policy.set]', self.read_config())

    def test_port_change_updates_all_proxy_values(self):
        self.assertTrue(self.sync(17890)['ok'])
        self.assertTrue(self.sync(19001)['ok'])

        text = self.read_config()
        self.assertNotIn('127.0.0.1:17890', text)
        self.assertGreaterEqual(text.count('127.0.0.1:19001'), 12)

    def test_repeated_sync_is_idempotent(self):
        self.assertTrue(self.sync(18080)['ok'])
        first = self.read_config()
        self.assertTrue(self.sync(18080)['ok'])
        self.assertEqual(first, self.read_config())

    def test_restore_returns_original_values_and_removes_added_fields(self):
        self.write_config(
            '[features]\n'
            'respect_system_proxy = false\n'
            '[mcp_servers.node_repl.env]\n'
            'HTTP_PROXY = "http://old:1"\n'
            'CUSTOM = "保留"\n')
        self.assertTrue(self.sync(18080)['ok'])
        result = self.restore()

        self.assertTrue(result['ok'])
        parsed = self.parse_config()
        self.assertIs(False, parsed['features']['respect_system_proxy'])
        self.assertEqual(
            parsed['mcp_servers']['node_repl']['env']['HTTP_PROXY'],
            'http://old:1')
        self.assertEqual(parsed['mcp_servers']['node_repl']['env']['CUSTOM'], '保留')
        self.assertNotIn('HTTP_PROXY', parsed['shell_environment_policy']['set'])
        self.assertFalse(os.path.exists(self.snapshot_path))

    def test_user_manual_change_is_not_overwritten_on_restore(self):
        self.assertTrue(self.sync(18080)['ok'])
        text = self.read_config().replace(
            'HTTP_PROXY = "http://127.0.0.1:18080"',
            'HTTP_PROXY = "http://manual:9"')
        self.write_config(text)

        result = self.restore()
        self.assertTrue(result['ok'])
        self.assertTrue(result['warnings'])
        self.assertEqual(
            self.parse_config()['mcp_servers']['node_repl']['env']['HTTP_PROXY'],
            'http://manual:9')

    def test_corrupt_toml_keeps_original_file(self):
        original = '[features]\nrespect_system_proxy = [\n'
        self.write_config(original)

        result = self.sync()
        self.assertFalse(result['ok'])
        self.assertEqual(original, self.read_config())

    def test_atomic_write_failure_keeps_original_file(self):
        original = '[features]\nrespect_system_proxy = false\n'
        self.write_config(original)
        with mock.patch.object(
                codex_proxy.os, 'replace', side_effect=OSError('中断')):
            result = self.sync()

        self.assertFalse(result['ok'])
        self.assertEqual(original, self.read_config())

    def test_permission_failure_keeps_original_file(self):
        original = '[features]\nrespect_system_proxy = false\n'
        self.write_config(original)
        with mock.patch.object(
                codex_proxy, '_atomic_write',
                side_effect=PermissionError('拒绝访问')):
            result = self.sync()

        self.assertFalse(result['ok'])
        self.assertEqual(original, self.read_config())

    def test_utf8_content_survives_sync_and_restore(self):
        original = (
            '# 中文 🇨🇳 é\n'
            '[mcp_servers.other]\n'
            'description = "测试配置"\n')
        self.write_config(original)
        self.assertTrue(self.sync(18123)['ok'])
        self.assertIn('# 中文 🇨🇳 é', self.read_config())
        self.assertIn('description = "测试配置"', self.read_config())
        self.assertTrue(self.restore()['ok'])
        self.assertIn('# 中文 🇨🇳 é', self.read_config())

    def parse_config(self):
        with open(self.config_path, 'rb') as stream:
            return tomllib.load(stream)


if __name__ == '__main__':
    unittest.main()
