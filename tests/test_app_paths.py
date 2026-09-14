# -*- coding: utf-8 -*-
import os
import pathlib
import tempfile
import time
import unittest

from core import (app_paths, config, config_maintenance, routing_rules,
                  subscription_store)


class AppPathsTest(unittest.TestCase):
    def test_user_data_root_uses_local_appdata(self):
        root = app_paths.user_data_root({
            'LOCALAPPDATA': r'D:\UserData\Local',
            'USERPROFILE': r'C:\Users\ignored',
        })

        self.assertEqual(root, r'D:\UserData\Local\CXVPNTools')

    def test_runtime_modules_share_the_user_data_root(self):
        root = app_paths.user_data_root()

        self.assertEqual(config.BASE, root)
        self.assertEqual(config.CFG_PATH, os.path.join(root, 'config.json'))
        self.assertEqual(
            subscription_store.cache_root(),
            os.path.join(root, 'routing', 'providers'))
        self.assertEqual(
            config_maintenance.ConfigHistory().root,
            os.path.join(root, 'routing', 'history'))
        self.assertEqual(
            routing_rules.RULE_PACK_DIR,
            os.path.join(root, 'rule-packs'))

    def test_migration_merges_portable_and_legacy_local_data(self):
        with tempfile.TemporaryDirectory() as temp:
            base = pathlib.Path(temp)
            portable = base / 'portable'
            internal = portable / '_internal'
            old_local = base / 'Local' / 'CXVPNManager'
            target = base / 'Local' / 'CXVPNTools'
            bundled = base / 'bundled-rule-packs'

            self._write(portable / 'config.json', '{"phone":"old"}')
            self._write(portable / 'run.log', 'old log')
            self._write(internal / 'startup.log', 'startup')
            self._write(
                internal / 'webview_data' / 'Default' / 'Cookies', 'cookie')
            self._write(
                portable / 'browser_data_probe' / 'state.json', 'browser')
            self._write(
                portable / 'routing' / 'providers' / 'portable.yaml', 'rp')
            self._write(
                old_local / 'routing' / 'providers' / 'provider.yaml', 'p')
            self._write(
                old_local / 'routing' / 'history' / 'history.json', 'h')
            self._write(
                portable / 'rule-packs' / 'local-direct-v1.txt', 'custom')
            self._write(bundled / 'local-direct-v1.txt', 'default local')
            self._write(bundled / 'cn-direct-v1.txt', 'default cn')
            self._write(target / 'run.log', 'current log')

            result = app_paths.migrate_legacy_user_data(
                data_root=str(target),
                legacy_roots=[str(portable), str(internal)],
                legacy_local_root=str(old_local),
                bundled_rule_pack_root=str(bundled))

            self.assertEqual(result['warnings'], [])
            self.assertEqual(
                (target / 'config.json').read_text(encoding='utf-8'),
                '{"phone":"old"}')
            self.assertEqual(
                (target / 'run.log').read_text(encoding='utf-8'),
                'current log')
            self.assertEqual(
                (target / 'startup.log').read_text(encoding='utf-8'),
                'startup')
            self.assertEqual(
                (target / 'webview_data' / 'Default' / 'Cookies').read_text(
                    encoding='utf-8'), 'cookie')
            self.assertEqual(
                (target / 'browser_data_probe' / 'state.json').read_text(
                    encoding='utf-8'), 'browser')
            self.assertTrue(
                (target / 'routing' / 'providers' / 'provider.yaml').is_file())
            self.assertTrue(
                (target / 'routing' / 'history' / 'history.json').is_file())
            self.assertTrue(
                (target / 'routing' / 'providers' / 'portable.yaml').is_file())
            self.assertEqual(
                (target / 'rule-packs' / 'local-direct-v1.txt').read_text(
                    encoding='utf-8'), 'custom')
            self.assertEqual(
                (target / 'rule-packs' / 'cn-direct-v1.txt').read_text(
                    encoding='utf-8'), 'default cn')
            self.assertTrue(result['copied'])

    def test_migration_uses_newer_valid_config_and_preserves_old_target(self):
        with tempfile.TemporaryDirectory() as temp:
            base = pathlib.Path(temp)
            portable = base / 'portable'
            target = base / 'Local' / 'CXVPNTools'
            old_value = '{"routing":{"proxy_providers":[]}}'
            new_value = (
                '{"routing":{"proxy_providers":['
                '{"id":"provider-new","name":"便宜机场🇨🇳é"}]}}')
            self._write(target / 'config.json', old_value)
            self._write(portable / 'config.json', new_value)
            now = time.time()
            os.utime(target / 'config.json', (now - 60, now - 60))
            os.utime(portable / 'config.json', (now, now))

            result = app_paths.migrate_legacy_user_data(
                data_root=str(target), legacy_roots=[str(portable)],
                legacy_local_root=str(base / 'missing-old-local'),
                bundled_rule_pack_root=str(base / 'missing-rule-packs'))

            self.assertEqual(result['warnings'], [])
            self.assertEqual(
                (target / 'config.json').read_text(encoding='utf-8'),
                new_value)
            self.assertEqual(result['replaced'], [str(target / 'config.json')])
            self.assertEqual(len(result['conflicts']), 1)
            backup = pathlib.Path(result['conflicts'][0]['backup'])
            self.assertEqual(backup.read_text(encoding='utf-8'), old_value)

    def test_migration_keeps_newer_target_and_preserves_legacy_config(self):
        with tempfile.TemporaryDirectory() as temp:
            base = pathlib.Path(temp)
            portable = base / 'portable'
            target = base / 'Local' / 'CXVPNTools'
            old_value = '{"phone":"legacy"}'
            new_value = '{"phone":"current"}'
            self._write(portable / 'config.json', old_value)
            self._write(target / 'config.json', new_value)
            now = time.time()
            os.utime(portable / 'config.json', (now - 60, now - 60))
            os.utime(target / 'config.json', (now, now))

            result = app_paths.migrate_legacy_user_data(
                data_root=str(target), legacy_roots=[str(portable)],
                legacy_local_root=str(base / 'missing-old-local'),
                bundled_rule_pack_root=str(base / 'missing-rule-packs'))

            self.assertEqual(result['warnings'], [])
            self.assertEqual(
                (target / 'config.json').read_text(encoding='utf-8'),
                new_value)
            self.assertEqual(result['replaced'], [])
            backup = pathlib.Path(result['conflicts'][0]['backup'])
            self.assertEqual(backup.read_text(encoding='utf-8'), old_value)

    def test_migration_never_replaces_config_after_state_database_exists(self):
        with tempfile.TemporaryDirectory() as temp:
            base = pathlib.Path(temp)
            portable = base / 'portable'
            target = base / 'Local' / 'CXVPNTools'
            current_value = '{"phone":"current"}'
            legacy_value = '{"phone":"newer legacy"}'
            self._write(target / 'config.json', current_value)
            self._write(target / app_paths.STATE_DATABASE_NAME, 'database')
            self._write(portable / 'config.json', legacy_value)
            now = time.time()
            os.utime(target / 'config.json', (now - 60, now - 60))
            os.utime(portable / 'config.json', (now, now))

            result = app_paths.migrate_legacy_user_data(
                data_root=str(target), legacy_roots=[str(portable)],
                legacy_local_root=str(base / 'missing-old-local'),
                bundled_rule_pack_root=str(base / 'missing-rule-packs'))

            self.assertEqual(result['warnings'], [])
            self.assertEqual(
                (target / 'config.json').read_text(encoding='utf-8'),
                current_value)
            self.assertEqual(result['replaced'], [])
            self.assertEqual(
                result['conflicts'][0]['reason'], 'canonical-state-present')
            backup = pathlib.Path(result['conflicts'][0]['backup'])
            self.assertEqual(backup.read_text(encoding='utf-8'), legacy_value)

    def test_migration_never_replaces_valid_target_with_invalid_config(self):
        with tempfile.TemporaryDirectory() as temp:
            base = pathlib.Path(temp)
            portable = base / 'portable'
            target = base / 'Local' / 'CXVPNTools'
            current_value = '{"phone":"current"}'
            self._write(target / 'config.json', current_value)
            self._write(portable / 'config.json', '{broken')
            now = time.time()
            os.utime(target / 'config.json', (now - 60, now - 60))
            os.utime(portable / 'config.json', (now, now))

            result = app_paths.migrate_legacy_user_data(
                data_root=str(target), legacy_roots=[str(portable)],
                legacy_local_root=str(base / 'missing-old-local'),
                bundled_rule_pack_root=str(base / 'missing-rule-packs'))

            self.assertEqual(result['warnings'], [])
            self.assertEqual(
                (target / 'config.json').read_text(encoding='utf-8'),
                current_value)
            self.assertEqual(result['replaced'], [])
            self.assertEqual(
                result['conflicts'][0]['reason'], 'source-invalid')
            backup = pathlib.Path(result['conflicts'][0]['backup'])
            self.assertEqual(backup.read_text(encoding='utf-8'), '{broken')

    def test_migration_reports_blocker_when_conflict_backup_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            base = pathlib.Path(temp)
            portable = base / 'portable'
            target = base / 'Local' / 'CXVPNTools'
            current_value = '{"phone":"current"}'
            self._write(target / 'config.json', current_value)
            self._write(portable / 'config.json', '{"phone":"newer"}')
            self._write(target / app_paths.MIGRATION_BACKUP_DIR, 'occupied')
            now = time.time()
            os.utime(target / 'config.json', (now - 60, now - 60))
            os.utime(portable / 'config.json', (now, now))

            result = app_paths.migrate_legacy_user_data(
                data_root=str(target), legacy_roots=[str(portable)],
                legacy_local_root=str(base / 'missing-old-local'),
                bundled_rule_pack_root=str(base / 'missing-rule-packs'))

            self.assertEqual(
                (target / 'config.json').read_text(encoding='utf-8'),
                current_value)
            self.assertEqual(result['replaced'], [])
            self.assertEqual(
                result['conflicts'][0]['reason'], 'backup-failed')
            self.assertEqual(len(result['blocking']), 1)

    @staticmethod
    def _write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding='utf-8')


if __name__ == '__main__':
    unittest.main()
