# -*- coding: utf-8 -*-
import json
import tempfile
import unittest

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

    def test_diagnostic_redacts_urls_credentials_and_connection_targets(self):
        result = config_maintenance.diagnostic_bundle(
            sample_config(),
            ['request https://secret.example/path?token=abc',
             'Authorization: Bearer private-token'],
            [{'payload': 'proxy https://sub.example/path?key=abc'}],
            {'secret': 'controller-value', 'mihomo_log': [
                'connect api.private.example:443 via 203.0.113.8:443']},
            {'active_connection_count': 2})

        text = result['content']
        self.assertNotIn('secret.example', text)
        self.assertNotIn('sub.example', text)
        self.assertNotIn('private-token', text)
        self.assertNotIn('controller-value', text)
        self.assertNotIn('api.private.example', text)
        self.assertNotIn('203.0.113.8', text)
        self.assertIn('active_connection_count', text)


class ConfigHistoryTests(unittest.TestCase):
    def test_history_is_bounded_and_only_success_is_restorable(self):
        with tempfile.TemporaryDirectory() as root:
            history = config_maintenance.ConfigHistory(root)
            config = sample_config()['routing']
            history.record(config, True, 'manual', '成功')
            history.record(config, False, 'manual', '失败 token=secret')

            items = history.list()

            self.assertEqual(len(items), 2)
            self.assertFalse(items[0]['restorable'])
            self.assertNotIn('secret', items[0]['message'])
            restored = history.load(items[1]['id'])
            self.assertEqual(restored['proxy_providers'][0]['id'], 'alpha')


if __name__ == '__main__':
    unittest.main()
