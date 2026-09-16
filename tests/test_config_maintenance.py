# -*- coding: utf-8 -*-
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from core import config_maintenance
from core import routing


def sample_config():
    return {
        'phone': '13800138000',
        'vpn_name': '公司 VPN',
        'creds': {'公司 VPN': {'user': 'alice', 'pass': 'vpn-secret'}},
        'authorization': {'last_success_at': 1},
        'sms': {'email': {'username': 'mail@example.test', 'password': 'mail-secret'}},
        'vlm': {'base': 'https://ai.example.test', 'key': 'ai-secret'},
        'routing': routing.normalize_config({
            **routing.default_config(),
            'proxy_providers': [{
                'id': 'alpha', 'name': '订阅一',
                'url': 'https://sub.example.test/path?token=private',
                'enabled': True, 'download_route': 'custom-proxy',
                'download_proxy': 'http://127.0.0.1:7897',
            }],
            'default_outbound': 'proxy:alpha',
            'rules': [{'domain': 'example.com', 'outbound': 'physical'}],
        }),
    }


class ConfigBackupTests(unittest.TestCase):
    def test_file_round_trip_preserves_unicode(self):
        current = sample_config()
        name = '中文-e\u0301-\U0001f1e8\U0001f1f3'
        current['routing']['proxy_providers'][0]['name'] = name
        result = config_maintenance.create_backup(current, True)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / (name + '.json')
            saved = config_maintenance.write_backup_file(path, result['content'])
            content = config_maintenance.read_backup_file(saved)
            prepared = config_maintenance.prepare_restore(content, current)
            self.assertEqual(path.read_bytes(), result['content'].encode('utf-8'))
            self.assertEqual(prepared['candidate']['routing']['proxy_providers'][0]['name'], name)
            self.assertEqual(list(Path(root).iterdir()), [path])

    def test_failed_replace_preserves_existing_export_and_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / '配置.json'
            path.write_text('原文件', encoding='utf-8')
            with mock.patch.object(config_maintenance.os, 'replace', side_effect=PermissionError('locked')):
                with self.assertRaises(PermissionError):
                    config_maintenance.write_backup_file(path, '新配置')
            self.assertEqual(path.read_text(encoding='utf-8'), '原文件')
            self.assertEqual(list(Path(root).iterdir()), [path])

    def test_import_accepts_utf8_bom_and_rejects_invalid_files(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / '配置.JSON'
            path.write_bytes(b'\xef\xbb\xbf' + '{"备注":"中文"}'.encode('utf-8'))
            self.assertEqual(config_maintenance.read_backup_file(path), '{"备注":"中文"}')
            path.write_bytes(b'x' * (config_maintenance.MAX_IMPORT_BYTES + 1))
            with self.assertRaisesRegex(routing.RoutingError, '2 MB'):
                config_maintenance.read_backup_file(path)
            path.write_bytes(b'\xff')
            with self.assertRaises(UnicodeDecodeError):
                config_maintenance.read_backup_file(path)
            with self.assertRaisesRegex(routing.RoutingError, 'JSON'):
                config_maintenance.read_backup_file(str(path) + '.txt')

    def test_default_backup_omits_credentials_and_subscription_urls(self):
        result = config_maintenance.create_backup(sample_config())
        text = result['content']
        document = json.loads(text)

        self.assertNotIn('vpn-secret', text)
        self.assertNotIn('mail-secret', text)
        self.assertNotIn('ai-secret', text)
        self.assertNotIn('13800138000', text)
        self.assertNotIn('sub.example.test', text)
        self.assertNotIn('download_proxy', text)
        self.assertFalse(document['credentials_included'])
        self.assertNotIn('url', document['config']['routing']['proxy_providers'][0])

    def test_explicit_url_backup_still_omits_other_secrets(self):
        result = config_maintenance.create_backup(
            sample_config(), include_subscription_urls=True)
        text = result['content']

        self.assertIn('sub.example.test', text)
        self.assertNotIn('vpn-secret', text)
        self.assertNotIn('mail-secret', text)
        self.assertNotIn('download_proxy', text)

    def test_restore_preserves_local_secrets_urls_and_enabled_state(self):
        current = sample_config()
        backup = config_maintenance.create_backup(current)['content']
        document = json.loads(backup)
        document['config']['routing']['traffic_mode'] = 'global'
        document['config']['routing']['enabled'] = False
        current['routing']['enabled'] = True

        prepared = config_maintenance.prepare_restore(
            json.dumps(document, ensure_ascii=False), current)

        candidate = prepared['candidate']
        self.assertEqual(candidate['creds']['公司 VPN']['pass'], 'vpn-secret')
        self.assertEqual(
            candidate['routing']['proxy_providers'][0]['url'],
            'https://sub.example.test/path?token=private')
        self.assertTrue(candidate['routing']['enabled'])
        self.assertEqual(candidate['routing']['traffic_mode'], 'global')
        self.assertTrue(prepared['summary']['credentials_preserved'])


if __name__ == '__main__':
    unittest.main()
