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

    @property
    def transport_snapshot_path(self):
        return os.path.join(
            self.data_root, codex_proxy.TRANSPORT_SNAPSHOT_FILE_NAME)

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

    def set_wss(self, enabled):
        return codex_proxy.set_websocket_enabled(
            enabled, environ=self.environ, data_root=self.data_root)

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

    def test_restore_removes_managed_fields_and_keeps_others(self):
        self.write_config(
            '# 保留注释\n'
            '[features]\n'
            'respect_system_proxy = false # 原值\n'
            '[mcp_servers.node_repl.env]\n'
            'HTTP_PROXY = "http://old:1"\n'
            'CUSTOM = "保留"\n')
        self.assertTrue(self.sync(18080)['ok'])
        result = self.restore()

        self.assertTrue(result['ok'])
        text = self.read_config()
        parsed = self.parse_config()
        self.assertIn('# 保留注释', text)
        self.assertNotIn('respect_system_proxy', parsed['features'])
        self.assertNotIn(
            'HTTP_PROXY', parsed['mcp_servers']['node_repl']['env'])
        self.assertEqual(parsed['mcp_servers']['node_repl']['env']['CUSTOM'], '保留')
        self.assertFalse(os.path.exists(self.snapshot_path))

    def test_restore_removes_created_sections_fields(self):
        self.assertTrue(self.sync(18080)['ok'])

        self.assertTrue(self.restore()['ok'])
        parsed = self.parse_config()
        self.assertNotIn('respect_system_proxy', parsed['features'])
        self.assertFalse(parsed['mcp_servers']['node_repl']['env'])
        self.assertFalse(parsed['shell_environment_policy']['set'])

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

    def test_read_config_returns_original_text(self):
        original = '# 中文 🇨🇳\n[features]\nrespect_system_proxy = false\n'
        self.write_config(original)

        result = codex_proxy.read_config(environ=self.environ)

        self.assertTrue(result['ok'])
        self.assertTrue(result['exists'])
        self.assertEqual(original, result['text'])
        self.assertEqual(self.config_path, result['path'])

    def test_read_config_missing_file_reports_not_exists(self):
        result = codex_proxy.read_config(environ=self.environ)

        self.assertTrue(result['ok'])
        self.assertFalse(result['exists'])
        self.assertEqual('', result['text'])

    def test_read_config_oversize_file_is_refused(self):
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        with open(self.config_path, 'wb') as stream:
            stream.write(b'# pad\n' * 200000)

        result = codex_proxy.read_config(environ=self.environ)

        self.assertFalse(result['ok'])
        self.assertIn('上限', result['warning'])

    def test_status_reports_snapshot_and_deviation(self):
        self.write_config(
            '[features]\nrespect_system_proxy = true\n'
            '[mcp_servers.node_repl.env]\nHTTP_PROXY = "http://old:1"\n')
        self.assertTrue(self.sync()['ok'])

        state = codex_proxy.status(
            environ=self.environ, data_root=self.data_root)
        self.assertTrue(state['ok'])
        self.assertTrue(state['snapshot_exists'])
        self.assertGreater(state['synced_fields'], 0)
        self.assertEqual(17890, state['last_mixed_port'])
        self.assertEqual([], state['deviated_fields'])

        text = self.read_config().replace(
            'HTTP_PROXY = "http://127.0.0.1:17890"',
            'HTTP_PROXY = "http://manual:9"')
        self.write_config(text)
        state = codex_proxy.status(
            environ=self.environ, data_root=self.data_root)
        self.assertIn(
            'mcp_servers.node_repl.env.HTTP_PROXY', state['deviated_fields'])

    def test_status_without_snapshot_is_safe(self):
        self.write_config('[other]\nvalue = "中文"\n')

        state = codex_proxy.status(
            environ=self.environ, data_root=self.data_root)

        self.assertTrue(state['ok'])
        self.assertFalse(state['snapshot_exists'])
        self.assertEqual(0, state['synced_fields'])
        self.assertEqual(0, state['last_mixed_port'])

    def test_status_with_corrupt_config_reports_parse_error(self):
        self.write_config('[features]\nrespect_system_proxy = [\n')

        state = codex_proxy.status(
            environ=self.environ, data_root=self.data_root)

        self.assertTrue(state['ok'])
        self.assertTrue(state['parse_error'])

    def test_status_reports_residual_managed_fields_without_snapshot(self):
        self.write_config(
            '[features]\nrespect_system_proxy = true\n'
            '[mcp_servers.node_repl.env]\n'
            'HTTP_PROXY = "http://127.0.0.1:17890"\n')

        state = codex_proxy.status(
            environ=self.environ, data_root=self.data_root)

        self.assertTrue(state['ok'])
        self.assertIn('features.respect_system_proxy', state['residual_fields'])
        self.assertIn(
            'mcp_servers.node_repl.env.HTTP_PROXY', state['residual_fields'])

    def test_restore_without_snapshot_removes_residual_managed_fields(self):
        self.write_config(
            '# 保留注释\n'
            '[features]\nrespect_system_proxy = true\n'
            '[mcp_servers.node_repl.env]\n'
            'HTTP_PROXY = "http://127.0.0.1:17890"\n'
            'CUSTOM = "保留"\n')

        result = self.restore()

        self.assertTrue(result['ok'])
        self.assertTrue(result.get('changed'))
        text = self.read_config()
        parsed = self.parse_config()
        self.assertIn('# 保留注释', text)
        self.assertNotIn('respect_system_proxy', parsed['features'])
        self.assertNotIn(
            'HTTP_PROXY', parsed['mcp_servers']['node_repl']['env'])
        self.assertEqual(parsed['mcp_servers']['node_repl']['env']['CUSTOM'], '保留')

    def test_restore_without_snapshot_and_no_managed_fields_is_noop(self):
        original = '[other]\nvalue = "中文"\n'
        self.write_config(original)

        result = self.restore()

        self.assertTrue(result['ok'])
        self.assertFalse(result.get('changed'))
        self.assertEqual(original, self.read_config())

    def test_transport_defaults_to_wss_preferred_without_config(self):
        state = codex_proxy.transport_status(
            environ=self.environ, data_root=self.data_root)

        self.assertEqual('wss_preferred', state['transport_mode'])
        self.assertIs(True, state['websocket_enabled'])
        self.assertEqual('openai', state['active_provider'])
        self.assertFalse(state['transport_managed'])

    def test_transport_http_only_adds_managed_provider_and_preserves_others(self):
        self.write_config(
            '# 中文 🇨🇳 é\n'
            'model_provider = "openai"\n\n'
            '[model_providers.openai_http]\n'
            'name = "用户 Provider"\n'
            'base_url = "https://example.test"\n')

        result = self.set_wss(False)

        self.assertTrue(result['ok'])
        self.assertTrue(result['changed'])
        parsed = self.parse_config()
        self.assertEqual(codex_proxy.HTTP_PROVIDER_ID, parsed['model_provider'])
        self.assertEqual(
            codex_proxy.HTTP_PROVIDER,
            parsed['model_providers'][codex_proxy.HTTP_PROVIDER_ID])
        self.assertEqual(
            '用户 Provider', parsed['model_providers']['openai_http']['name'])
        self.assertIn('# 中文 🇨🇳 é', self.read_config())
        self.assertTrue(os.path.isfile(self.transport_snapshot_path))
        state = codex_proxy.transport_status(
            environ=self.environ, data_root=self.data_root)
        self.assertEqual('http_only', state['transport_mode'])
        self.assertIs(False, state['websocket_enabled'])
        self.assertTrue(state['transport_managed'])

    def test_transport_repeated_http_only_is_idempotent(self):
        self.assertTrue(self.set_wss(False)['ok'])
        first = self.read_config()

        result = self.set_wss(False)

        self.assertTrue(result['ok'])
        self.assertFalse(result['changed'])
        self.assertEqual(first, self.read_config())

    def test_transport_restore_restores_explicit_openai_and_removes_provider(self):
        self.write_config('model_provider = "openai"\n[other]\nvalue = "保留"\n')
        self.assertTrue(self.set_wss(False)['ok'])

        result = self.set_wss(True)

        self.assertTrue(result['ok'])
        parsed = self.parse_config()
        self.assertEqual('openai', parsed['model_provider'])
        self.assertEqual('保留', parsed['other']['value'])
        self.assertNotIn(codex_proxy.HTTP_PROVIDER_ID, parsed.get('model_providers', {}))
        self.assertFalse(os.path.exists(self.transport_snapshot_path))

    def test_transport_restore_removes_added_root_key_when_original_missing(self):
        self.write_config('[other]\nvalue = "保留"\n')
        self.assertTrue(self.set_wss(False)['ok'])

        self.assertTrue(self.set_wss(True)['ok'])

        parsed = self.parse_config()
        self.assertNotIn('model_provider', parsed)
        self.assertNotIn(codex_proxy.HTTP_PROVIDER_ID, parsed.get('model_providers', {}))

    def test_transport_rejects_custom_provider_and_same_id_collision(self):
        for config in (
                'model_provider = "cm"\n[model_providers.cm]\nname = "CM"\n',
                'model_provider = "openai"\n'
                f'[model_providers.{codex_proxy.HTTP_PROVIDER_ID}]\nname = "用户定义"\n'):
            with self.subTest(config=config):
                self.write_config(config)
                result = self.set_wss(False)
                self.assertFalse(result['ok'])
                self.assertEqual(config, self.read_config())
                if os.path.exists(self.transport_snapshot_path):
                    os.unlink(self.transport_snapshot_path)

    def test_transport_rejects_unsupported_source_and_complex_toml(self):
        for config in (
                'openai_base_url = "https://example.test"\n',
                'profile = "work"\n',
                '[model_providers.openai]\nsupports_websockets = false\n',
                '"model_provider" = "openai"\n',
                f'model_providers = {{ {codex_proxy.HTTP_PROVIDER_ID} = {{ name = "x" }} }}\n'):
            with self.subTest(config=config):
                self.write_config(config)
                result = self.set_wss(False)
                self.assertFalse(result['ok'])
                self.assertEqual(config, self.read_config())

    def test_transport_restore_preserves_manually_modified_provider(self):
        self.write_config('model_provider = "openai"\n')
        self.assertTrue(self.set_wss(False)['ok'])
        self.write_config(self.read_config().replace(
            'name = "OpenAI HTTP via CXVPN"', 'name = "用户已修改"'))

        result = self.set_wss(True)

        self.assertTrue(result['ok'])
        self.assertTrue(result['warnings'])
        parsed = self.parse_config()
        self.assertEqual('openai', parsed['model_provider'])
        self.assertEqual(
            '用户已修改',
            parsed['model_providers'][codex_proxy.HTTP_PROVIDER_ID]['name'])
        self.assertFalse(os.path.exists(self.transport_snapshot_path))

    def test_transport_user_changed_active_provider_causes_conflict(self):
        self.write_config('model_provider = "openai"\n')
        self.assertTrue(self.set_wss(False)['ok'])
        self.write_config(self.read_config().replace(
            f'model_provider = "{codex_proxy.HTTP_PROVIDER_ID}"',
            'model_provider = "cm"'))

        result = self.set_wss(True)

        self.assertFalse(result['ok'])
        self.assertEqual('cm', self.parse_config()['model_provider'])
        self.assertTrue(codex_proxy.transport_status(
            environ=self.environ, data_root=self.data_root)['transport_conflict'])

    def test_transport_restore_remains_available_after_unrelated_profile_change(self):
        self.write_config('model_provider = "openai"\n')
        self.assertTrue(self.set_wss(False)['ok'])
        self.write_config('profile = "work"\n' + self.read_config())

        result = self.set_wss(True)

        self.assertTrue(result['ok'])
        parsed = self.parse_config()
        self.assertEqual('openai', parsed['model_provider'])
        self.assertEqual('work', parsed['profile'])

    def test_transport_prepared_snapshot_is_reconciled_without_guessing(self):
        self.write_config('model_provider = "openai"\n')
        os.makedirs(self.data_root, exist_ok=True)
        record = {
            'version': codex_proxy.TRANSPORT_SNAPSHOT_VERSION,
            'config_path': self.config_path,
            'phase': 'prepared',
            'original_model_provider_exists': True,
            'original_model_provider': 'openai',
            'provider_created': True,
            'last_written': {
                'model_provider': codex_proxy.HTTP_PROVIDER_ID,
                'provider': codex_proxy.HTTP_PROVIDER,
            },
        }
        with open(self.transport_snapshot_path, 'w', encoding='utf-8') as stream:
            __import__('json').dump(record, stream, ensure_ascii=False)

        state = codex_proxy.transport_status(
            environ=self.environ, data_root=self.data_root)
        self.assertEqual('wss_preferred', state['transport_mode'])
        self.assertFalse(state['transport_conflict'])
        self.assertTrue(self.set_wss(False)['ok'])
        self.assertEqual('http_only', codex_proxy.transport_status(
            environ=self.environ, data_root=self.data_root)['transport_mode'])

    def test_transport_config_write_failure_restores_snapshot_state(self):
        self.write_config('model_provider = "openai"\n')
        original = self.read_config()
        with mock.patch.object(
                codex_proxy, '_write_config_if_unchanged',
                side_effect=OSError('中断')):
            result = self.set_wss(False)

        self.assertFalse(result['ok'])
        self.assertEqual(original, self.read_config())
        self.assertFalse(os.path.exists(self.transport_snapshot_path))

    def test_transport_codex_home_change_refuses_old_snapshot(self):
        self.write_config('model_provider = "openai"\n')
        self.assertTrue(self.set_wss(False)['ok'])
        changed = dict(self.environ, CODEX_HOME=os.path.join(self.root, 'other-codex'))

        result = codex_proxy.set_websocket_enabled(
            True, environ=changed, data_root=self.data_root)

        self.assertFalse(result['ok'])
        self.assertIn('冲突', result['warning'])

    def test_transport_strict_boolean_validation(self):
        for value in ('false', 0, None):
            with self.subTest(value=value):
                result = codex_proxy.set_websocket_enabled(
                    value, environ=self.environ, data_root=self.data_root)
                self.assertFalse(result['ok'])
                self.assertFalse(os.path.exists(self.config_path))

    def test_proxy_and_transport_settings_are_independent(self):
        self.write_config('model_provider = "openai"\n')
        self.assertTrue(self.sync(18080)['ok'])
        self.assertTrue(self.set_wss(False)['ok'])

        self.assertTrue(self.restore()['ok'])
        parsed = self.parse_config()
        self.assertEqual(codex_proxy.HTTP_PROVIDER_ID, parsed['model_provider'])
        self.assertEqual(
            codex_proxy.HTTP_PROVIDER,
            parsed['model_providers'][codex_proxy.HTTP_PROVIDER_ID])
        self.assertNotIn('respect_system_proxy', parsed.get('features', {}))

        self.assertTrue(self.set_wss(True)['ok'])
        parsed = self.parse_config()
        self.assertEqual('openai', parsed['model_provider'])
        self.assertNotIn(codex_proxy.HTTP_PROVIDER_ID, parsed.get('model_providers', {}))

    def parse_config(self):
        with open(self.config_path, 'rb') as stream:
            return tomllib.load(stream)


if __name__ == '__main__':
    unittest.main()
