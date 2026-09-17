# -*- coding: utf-8 -*-
import os
import json
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

    @property
    def custom_api_snapshot_path(self):
        return os.path.join(
            self.data_root, codex_proxy.CUSTOM_API_SNAPSHOT_FILE_NAME)

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

    def save_custom_api(self, base_url='http://192.0.2.10:53142/v1', api_key='test-key-private'):
        return codex_proxy.save_custom_api(
            base_url, api_key, environ=self.environ, data_root=self.data_root)

    def restore_custom_api(self):
        return codex_proxy.restore_custom_api(
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

    def test_transport_user_changed_active_provider_can_restore_from_snapshot(self):
        self.write_config('model_provider = "openai"\n')
        self.assertTrue(self.set_wss(False)['ok'])
        self.write_config(self.read_config().replace(
            f'model_provider = "{codex_proxy.HTTP_PROVIDER_ID}"',
            'model_provider = "cm"'))

        state = codex_proxy.transport_status(
            environ=self.environ, data_root=self.data_root)
        self.assertEqual('custom_provider', state['transport_mode'])
        self.assertFalse(state['transport_conflict'])
        self.assertTrue(state['transport_restore_available'])

        result = self.set_wss(True)

        self.assertTrue(result['ok'])
        self.assertEqual('openai', self.parse_config()['model_provider'])
        self.assertFalse(os.path.exists(self.transport_snapshot_path))

    def test_transport_root_already_restored_allows_cleanup(self):
        self.write_config('model_provider = "openai"\n')
        self.assertTrue(self.set_wss(False)['ok'])
        self.write_config(self.read_config().replace(
            f'model_provider = "{codex_proxy.HTTP_PROVIDER_ID}"',
            'model_provider = "openai"'))

        state = codex_proxy.transport_status(
            environ=self.environ, data_root=self.data_root)
        self.assertEqual('wss_preferred', state['transport_mode'])
        self.assertIs(True, state['websocket_enabled'])
        self.assertTrue(state['transport_cleanup_pending'])
        self.assertTrue(state['transport_restore_available'])

        result = self.set_wss(True)

        self.assertTrue(result['ok'])
        self.assertNotIn(
            codex_proxy.HTTP_PROVIDER_ID,
            self.parse_config().get('model_providers', {}))
        self.assertFalse(os.path.exists(self.transport_snapshot_path))

    def test_transport_root_already_restored_can_switch_back_to_http(self):
        self.write_config('model_provider = "openai"\n')
        self.assertTrue(self.set_wss(False)['ok'])
        self.write_config(self.read_config().replace(
            f'model_provider = "{codex_proxy.HTTP_PROVIDER_ID}"',
            'model_provider = "openai"'))

        result = self.set_wss(False)

        self.assertTrue(result['ok'])
        self.assertTrue(result['changed'])
        self.assertEqual(
            codex_proxy.HTTP_PROVIDER_ID,
            self.parse_config()['model_provider'])
        self.assertTrue(os.path.exists(self.transport_snapshot_path))

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

    def test_custom_api_save_writes_key_but_snapshot_and_status_are_redacted(self):
        self.write_config('# 中文 🇨🇳 é\n[other]\nvalue = "保留"\n')

        result = self.save_custom_api()

        self.assertTrue(result['ok'])
        self.assertTrue(result['changed'])
        parsed = self.parse_config()
        provider = parsed['model_providers'][codex_proxy.CUSTOM_API_PROVIDER_ID]
        self.assertEqual(codex_proxy.CUSTOM_API_PROVIDER_ID, parsed['model_provider'])
        self.assertEqual('http://192.0.2.10:53142/v1', provider['base_url'])
        self.assertEqual('responses', provider['wire_api'])
        self.assertTrue(provider['requires_openai_auth'])
        self.assertFalse(provider['supports_websockets'])
        self.assertEqual('test-key-private', provider['experimental_bearer_token'])
        self.assertNotIn(
            codex_proxy.CUSTOM_API_LEGACY_PROVIDER_ID,
            parsed['model_providers'])
        with open(self.custom_api_snapshot_path, encoding='utf-8') as stream:
            snapshot_text = stream.read()
        self.assertNotIn('test-key-private', snapshot_text)
        self.assertIn('bearer_token_sha256', snapshot_text)
        state = codex_proxy.custom_api_status(
            environ=self.environ, data_root=self.data_root)
        self.assertTrue(state['custom_api_managed'])
        self.assertTrue(state['custom_api_active'])
        self.assertFalse(state['custom_api_migration_required'])
        self.assertTrue(state['custom_api_key_configured'])
        self.assertTrue(state['custom_api_openai_auth_required'])
        transport = codex_proxy.transport_status(
            environ=self.environ, data_root=self.data_root)
        self.assertEqual('http_only', transport['transport_mode'])
        self.assertFalse(transport['websocket_enabled'])
        self.assertTrue(transport['transport_managed'])
        self.assertNotIn('test-key-private', repr(state))
        revealed = codex_proxy.read_custom_api_key(
            environ=self.environ, data_root=self.data_root)
        self.assertTrue(revealed['ok'])
        self.assertTrue(revealed['key_configured'])
        self.assertEqual('test-key-private', revealed['api_key'])
        self.assertIn('# 中文 🇨🇳 é', self.read_config())

    def test_custom_api_key_echo_rejects_manually_modified_provider(self):
        self.assertTrue(self.save_custom_api()['ok'])
        self.write_config(self.read_config().replace(
            'experimental_bearer_token = "test-key-private"',
            'experimental_bearer_token = "manual-key"'))

        result = codex_proxy.read_custom_api_key(
            environ=self.environ, data_root=self.data_root)

        self.assertFalse(result['ok'])
        self.assertEqual('', result['api_key'])
        self.assertIn('已被修改', result['warning'])

    def test_custom_api_save_upgrades_managed_config_to_keep_openai_login(self):
        self.assertTrue(self.save_custom_api()['ok'])
        self.write_config(self.read_config().replace(
            'requires_openai_auth = true\n', ''))
        with open(self.custom_api_snapshot_path, encoding='utf-8') as stream:
            snapshot = json.load(stream)
        snapshot['last_written']['provider'].pop('requires_openai_auth')
        with open(
                self.custom_api_snapshot_path, 'w', encoding='utf-8') as stream:
            json.dump(snapshot, stream)

        before = codex_proxy.custom_api_status(
            environ=self.environ, data_root=self.data_root)
        self.assertTrue(before['custom_api_managed'])
        self.assertFalse(before['custom_api_conflict'])
        self.assertFalse(before['custom_api_openai_auth_required'])

        upgraded = self.save_custom_api(api_key='')

        self.assertTrue(upgraded['ok'])
        provider = self.parse_config()['model_providers'][
            codex_proxy.CUSTOM_API_PROVIDER_ID]
        self.assertTrue(provider['requires_openai_auth'])
        after = codex_proxy.custom_api_status(
            environ=self.environ, data_root=self.data_root)
        self.assertTrue(after['custom_api_managed'])
        self.assertTrue(after['custom_api_openai_auth_required'])

    def test_custom_api_blank_key_keeps_current_key_and_restore_recovers_original(self):
        self.write_config(
            'model_provider = "legacy"\n\n'
            '[model_providers.legacy]\nname = "Legacy"\n')
        self.assertTrue(self.save_custom_api(api_key='first-key')['ok'])

        result = self.save_custom_api(
            base_url='https://api.example.test/v1/', api_key='')

        self.assertTrue(result['ok'])
        provider = self.parse_config()['model_providers'][codex_proxy.CUSTOM_API_PROVIDER_ID]
        self.assertEqual('https://api.example.test/v1', provider['base_url'])
        self.assertEqual('first-key', provider['experimental_bearer_token'])
        restored = self.restore_custom_api()
        self.assertTrue(restored['ok'])
        parsed = self.parse_config()
        self.assertEqual('legacy', parsed['model_provider'])
        self.assertEqual('Legacy', parsed['model_providers']['legacy']['name'])
        self.assertNotIn(codex_proxy.CUSTOM_API_PROVIDER_ID, parsed['model_providers'])
        self.assertFalse(os.path.exists(self.custom_api_snapshot_path))

    def test_custom_api_restore_preserves_manually_modified_provider(self):
        self.assertTrue(self.save_custom_api()['ok'])
        self.write_config(self.read_config().replace(
            'base_url = "http://192.0.2.10:53142/v1"',
            'base_url = "https://manual.example/v1"'))

        result = self.restore_custom_api()

        self.assertTrue(result['ok'])
        self.assertTrue(result['warnings'])
        parsed = self.parse_config()
        self.assertNotIn('model_provider', parsed)
        self.assertEqual(
            'https://manual.example/v1',
            parsed['model_providers'][codex_proxy.CUSTOM_API_PROVIDER_ID]['base_url'])
        self.assertFalse(os.path.exists(self.custom_api_snapshot_path))

    def test_custom_api_automatically_hands_off_http_transport(self):
        self.assertTrue(self.set_wss(False)['ok'])

        result = self.save_custom_api()

        self.assertTrue(result['ok'])
        parsed = self.parse_config()
        self.assertEqual(codex_proxy.CUSTOM_API_PROVIDER_ID, parsed['model_provider'])
        self.assertNotIn(
            codex_proxy.HTTP_PROVIDER_ID, parsed.get('model_providers', {}))
        self.assertFalse(os.path.exists(self.transport_snapshot_path))
        self.assertTrue(self.restore_custom_api()['ok'])
        self.assertEqual('openai', self.parse_config().get('model_provider', 'openai'))

    def test_custom_api_handoff_preserves_current_custom_provider_for_restore(self):
        self.write_config(
            'model_provider = "openai"\n\n'
            '[model_providers.legacy]\nname = "Legacy"\n')
        self.assertTrue(self.set_wss(False)['ok'])
        self.write_config(self.read_config().replace(
            f'model_provider = "{codex_proxy.HTTP_PROVIDER_ID}"',
            'model_provider = "legacy"'))

        self.assertTrue(self.save_custom_api()['ok'])
        self.assertTrue(self.restore_custom_api()['ok'])

        parsed = self.parse_config()
        self.assertEqual('legacy', parsed['model_provider'])
        self.assertEqual('Legacy', parsed['model_providers']['legacy']['name'])

    def test_custom_api_handoff_rejects_modified_http_provider(self):
        self.assertTrue(self.set_wss(False)['ok'])
        self.write_config(self.read_config().replace(
            'name = "OpenAI HTTP via CXVPN"', 'name = "用户已修改"'))
        before = self.read_config()

        result = self.save_custom_api()

        self.assertFalse(result['ok'])
        self.assertIn('手动修改', result['warning'])
        self.assertEqual(before, self.read_config())

    def test_transport_updates_single_custom_api_provider(self):
        self.assertTrue(self.save_custom_api()['ok'])
        result = self.set_wss(True)

        self.assertTrue(result['ok'])
        self.assertTrue(result['changed'])
        parsed = self.parse_config()
        self.assertTrue(parsed['model_providers'][
            codex_proxy.CUSTOM_API_PROVIDER_ID]['supports_websockets'])
        self.assertNotIn(
            codex_proxy.CUSTOM_API_LEGACY_PROVIDER_ID,
            parsed['model_providers'])
        state = codex_proxy.transport_status(
            environ=self.environ, data_root=self.data_root)
        self.assertEqual('wss_preferred', state['transport_mode'])
        self.assertTrue(state['websocket_enabled'])

        repeated = self.set_wss(True)
        self.assertTrue(repeated['ok'])
        self.assertFalse(repeated['changed'])

    def test_custom_api_overwrites_and_restores_existing_local_provider(self):
        self.write_config(
            'model_provider = "codex_local_access"\n\n'
            '[model_providers.codex_local_access]\n'
            'name = "Codex API Service"\n'
            'base_url = "http://localhost:58555/v1"\n'
            'wire_api = "responses"\n'
            'requires_openai_auth = true\n'
            'experimental_bearer_token = "legacy-key"\n'
            'supports_websockets = false\n')

        saved = self.save_custom_api()

        self.assertTrue(saved['ok'])
        parsed = self.parse_config()
        provider = parsed['model_providers'][codex_proxy.CUSTOM_API_PROVIDER_ID]
        self.assertEqual('http://192.0.2.10:53142/v1', provider['base_url'])
        self.assertEqual('test-key-private', provider['experimental_bearer_token'])
        self.assertTrue(provider['requires_openai_auth'])

        restored = self.restore_custom_api()

        self.assertTrue(restored['ok'])
        parsed = self.parse_config()
        self.assertEqual('codex_local_access', parsed['model_provider'])
        provider = parsed['model_providers'][codex_proxy.CUSTOM_API_PROVIDER_ID]
        self.assertEqual('http://localhost:58555/v1', provider['base_url'])
        self.assertTrue(provider['requires_openai_auth'])
        self.assertEqual('legacy-key', provider['experimental_bearer_token'])

    def test_custom_api_never_rewrites_official_openai_provider(self):
        self.write_config(
            'model_provider = "openai"\n\n'
            '[model_providers.openai]\n'
            'name = "用户的官方渠道覆盖"\n'
            'base_url = "https://api.openai.com/v1"\n'
            'wire_api = "responses"\n')

        saved = self.save_custom_api()

        self.assertTrue(saved['ok'])
        parsed = self.parse_config()
        self.assertEqual(codex_proxy.CUSTOM_API_PROVIDER_ID, parsed['model_provider'])
        self.assertEqual(
            'https://api.openai.com/v1',
            parsed['model_providers']['openai']['base_url'])

        self.assertTrue(self.set_wss(True)['ok'])
        parsed = self.parse_config()
        self.assertEqual(codex_proxy.CUSTOM_API_PROVIDER_ID, parsed['model_provider'])
        self.assertEqual(
            'https://api.openai.com/v1',
            parsed['model_providers']['openai']['base_url'])

        restored = self.restore_custom_api()

        self.assertTrue(restored['ok'])
        parsed = self.parse_config()
        self.assertEqual('openai', parsed['model_provider'])
        self.assertEqual(
            'https://api.openai.com/v1',
            parsed['model_providers']['openai']['base_url'])
        self.assertNotIn(
            codex_proxy.CUSTOM_API_PROVIDER_ID, parsed['model_providers'])
        self.assertNotIn(
            codex_proxy.CUSTOM_API_LEGACY_PROVIDER_ID,
            parsed['model_providers'])

    def test_custom_api_upgrades_early_single_provider_v1_snapshot(self):
        provider = codex_proxy._custom_api_provider(
            'http://192.0.2.10:53142/v1', 'test-key-private')
        text = 'model_provider = "codex_local_access"\n'
        text = codex_proxy._append_provider(
            text, codex_proxy.CUSTOM_API_PROVIDER_ID, provider)
        self.write_config(text)
        snapshot = {
            'version': codex_proxy.CUSTOM_API_LEGACY_SNAPSHOT_VERSION,
            'config_path': self.config_path,
            'phase': 'committed',
            'original_model_provider_exists': True,
            'original_model_provider': 'openai',
            'provider_created': True,
            'last_written': {
                'model_provider': codex_proxy.CUSTOM_API_PROVIDER_ID,
                'provider': codex_proxy._custom_api_public_provider(provider),
                'bearer_token_sha256': codex_proxy._secret_sha256(
                    'test-key-private'),
            },
        }
        os.makedirs(self.data_root, exist_ok=True)
        with open(self.custom_api_snapshot_path, 'w', encoding='utf-8') as stream:
            json.dump(snapshot, stream)

        before = codex_proxy.custom_api_status(
            environ=self.environ, data_root=self.data_root)
        self.assertTrue(before['custom_api_managed'])
        self.assertFalse(before['custom_api_conflict'])
        self.assertTrue(before['custom_api_migration_required'])
        revealed = codex_proxy.read_custom_api_key(
            environ=self.environ, data_root=self.data_root)
        self.assertTrue(revealed['ok'])
        self.assertEqual('test-key-private', revealed['api_key'])

        result = self.save_custom_api(api_key='')

        self.assertTrue(result['ok'])
        with open(self.custom_api_snapshot_path, encoding='utf-8') as stream:
            upgraded = json.load(stream)
        self.assertEqual(
            codex_proxy.CUSTOM_API_SNAPSHOT_VERSION, upgraded['version'])
        state = codex_proxy.custom_api_status(
            environ=self.environ, data_root=self.data_root)
        self.assertTrue(state['custom_api_managed'])
        self.assertFalse(state['custom_api_conflict'])
        self.assertFalse(state['custom_api_migration_required'])

        self.assertTrue(self.restore_custom_api()['ok'])
        parsed = self.parse_config()
        self.assertEqual('openai', parsed['model_provider'])
        self.assertNotIn(
            codex_proxy.CUSTOM_API_PROVIDER_ID,
            parsed.get('model_providers', {}))

    def test_custom_api_repairs_partially_migrated_legacy_snapshot_on_resave(self):
        original = {
            'name': 'Codex API Service',
            'base_url': 'http://localhost:58555/v1',
            'wire_api': 'responses',
            'requires_openai_auth': True,
            'experimental_bearer_token': 'legacy-key',
            'supports_websockets': False,
        }
        primary = codex_proxy._custom_api_provider(
            'http://192.0.2.10:53142/v1', 'test-key-private')
        compat = codex_proxy._custom_api_provider(
            'http://192.0.2.10:53142/v1', 'test-key-private',
            name='CXVPN Custom API (Legacy Sessions)')
        text = 'model_provider = "cxvpn_custom_api"\n'
        text = codex_proxy._append_provider(
            text, codex_proxy.CUSTOM_API_LEGACY_PROVIDER_ID, primary)
        text = codex_proxy._append_provider(
            text, codex_proxy.CUSTOM_API_PROVIDER_ID, compat)
        self.write_config(text)
        snapshot = {
            'version': codex_proxy.CUSTOM_API_LEGACY_SNAPSHOT_VERSION,
            'config_path': self.config_path,
            'phase': 'committed',
            'original_model_provider_exists': True,
            'original_model_provider': 'codex_local_access',
            'provider_created': True,
            'original_compat_provider_exists': True,
            'original_compat_provider': original,
            'compat_provider_created': False,
            'last_written': {
                'model_provider': 'cxvpn_custom_api',
                'provider': codex_proxy._custom_api_public_provider(primary),
                'bearer_token_sha256': codex_proxy._secret_sha256(
                    'test-key-private'),
                'compat_provider': codex_proxy._custom_api_public_provider(
                    compat),
                'compat_bearer_token_sha256': codex_proxy._secret_sha256(
                    'test-key-private'),
            },
        }
        os.makedirs(self.data_root, exist_ok=True)
        with open(self.custom_api_snapshot_path, 'w', encoding='utf-8') as stream:
            json.dump(snapshot, stream)
        partial = codex_proxy._remove_provider(
            self.read_config(), codex_proxy.CUSTOM_API_LEGACY_PROVIDER_ID,
            '旧版自定义 API Provider')
        partial = codex_proxy._patch_root_key(
            partial, 'model_provider', codex_proxy.CUSTOM_API_PROVIDER_ID)
        self.write_config(partial)

        before = codex_proxy.custom_api_status(
            environ=self.environ, data_root=self.data_root)
        self.assertTrue(before['custom_api_managed'])
        self.assertFalse(before['custom_api_conflict'])
        self.assertTrue(before['custom_api_migration_required'])
        revealed = codex_proxy.read_custom_api_key(
            environ=self.environ, data_root=self.data_root)
        self.assertTrue(revealed['ok'])
        self.assertEqual('test-key-private', revealed['api_key'])
        result = self.save_custom_api(api_key='')

        self.assertTrue(result['ok'])
        parsed = self.parse_config()
        self.assertEqual('codex_local_access', parsed['model_provider'])
        self.assertNotIn(
            codex_proxy.CUSTOM_API_LEGACY_PROVIDER_ID,
            parsed['model_providers'])
        state = codex_proxy.custom_api_status(
            environ=self.environ, data_root=self.data_root)
        self.assertTrue(state['custom_api_managed'])
        self.assertFalse(state['custom_api_migration_required'])
        with open(self.custom_api_snapshot_path, encoding='utf-8') as stream:
            upgraded = json.load(stream)
        self.assertEqual(codex_proxy.CUSTOM_API_SNAPSHOT_VERSION,
                         upgraded['version'])

        self.assertTrue(self.restore_custom_api()['ok'])
        parsed = self.parse_config()
        self.assertEqual('codex_local_access', parsed['model_provider'])
        restored = parsed['model_providers'][codex_proxy.CUSTOM_API_PROVIDER_ID]
        self.assertEqual('http://localhost:58555/v1', restored['base_url'])
        self.assertEqual('legacy-key', restored['experimental_bearer_token'])

    def test_custom_api_rejects_invalid_input_and_unowned_legacy_id(self):
        for base_url, api_key in (('ftp://example.test/v1', 'key'),
                                  ('https://user:pass@example.test/v1', 'key'),
                                  ('https://example.test/v1?x=1', 'key'),
                                  ('https://example.test/v1', '')):
            with self.subTest(base_url=base_url, api_key=api_key):
                result = self.save_custom_api(base_url, api_key)
                self.assertFalse(result['ok'])
                self.assertFalse(os.path.exists(self.config_path))
        self.write_config(
            f'[model_providers.{codex_proxy.CUSTOM_API_LEGACY_PROVIDER_ID}]\n'
            'name = "用户定义"\n')
        before = self.read_config()
        result = self.save_custom_api()
        self.assertFalse(result['ok'])
        self.assertEqual(before, self.read_config())

    def test_custom_api_write_failure_restores_snapshot_state(self):
        self.write_config('model_provider = "openai"\n')
        original = self.read_config()
        with mock.patch.object(
                codex_proxy, '_write_config_if_unchanged',
                side_effect=OSError('中断')):
            result = self.save_custom_api()

        self.assertFalse(result['ok'])
        self.assertEqual(original, self.read_config())
        self.assertFalse(os.path.exists(self.custom_api_snapshot_path))

    def parse_config(self):
        with open(self.config_path, 'rb') as stream:
            return tomllib.load(stream)


if __name__ == '__main__':
    unittest.main()
