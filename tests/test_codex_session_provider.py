# -*- coding: utf-8 -*-
import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from core import codex_session_provider


class CodexSessionProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.codex_root = os.path.join(self.temp.name, 'codex')
        self.data_root = os.path.join(self.temp.name, 'cxvpn')
        os.makedirs(self.codex_root)
        os.makedirs(self.data_root)
        self.environ = {'CODEX_HOME': self.codex_root}

    def tearDown(self):
        self.temp.cleanup()

    def _write_config(self, provider):
        with open(os.path.join(self.codex_root, 'config.toml'), 'w',
                  encoding='utf-8', newline='\n') as stream:
            stream.write(f'model_provider = "{provider}"\n')

    def _rollout(self, thread_id, provider, source='cli'):
        folder = os.path.join(
            self.codex_root, 'sessions', '2026', '09', '17')
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f'rollout-{thread_id}.jsonl')
        first = {
            'type': 'session_meta',
            'payload': {
                'id': thread_id,
                'model_provider': provider,
                'source': source,
            },
        }
        with open(path, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(json.dumps(first, ensure_ascii=False) + '\n')
            stream.write(json.dumps({
                'type': 'event_msg',
                'payload': {
                    'text': '中文与 Unicode：' + chr(0x1F1E8) + chr(0x1F1F3),
                },
            }, ensure_ascii=False) + '\n')
        return path

    def _database(self, rows):
        path = os.path.join(self.codex_root, 'state_5.sqlite')
        connection = sqlite3.connect(path)
        connection.execute(
            'CREATE TABLE threads ('
            'id TEXT PRIMARY KEY, rollout_path TEXT, model_provider TEXT, '
            'archived INTEGER, preview TEXT, first_user_message TEXT, '
            'source TEXT, thread_source TEXT)')
        connection.executemany(
            'INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?)', rows)
        connection.commit()
        connection.close()
        return path

    @staticmethod
    def _first_provider(path):
        with open(path, 'r', encoding='utf-8') as stream:
            return json.loads(stream.readline())['payload']['model_provider']

    @staticmethod
    def _db_provider(path, thread_id):
        connection = sqlite3.connect(path)
        try:
            return connection.execute(
                'SELECT model_provider FROM threads WHERE id = ?',
                (thread_id,)).fetchone()[0]
        finally:
            connection.close()

    def test_migrates_only_visible_root_threads_and_is_bidirectional(self):
        self._write_config('codex_local_access')
        visible = self._rollout('visible-1', 'openai')
        archived = self._rollout('archived-1', 'openai')
        subagent = self._rollout('subagent-1', 'openai', {'subagent': {}})
        relative = lambda path: os.path.relpath(path, self.codex_root)
        db_path = self._database([
            ('visible-1', '\\\\?\\' + visible, 'openai', 0, 'hello',
             'hello', 'cli', 'user'),
            ('archived-1', relative(archived), 'openai', 1, 'hello',
             'hello', 'cli', 'user'),
            ('subagent-1', relative(subagent), 'openai', 0, 'hello',
             'hello', '{"subagent":{}}', 'subagent'),
        ])
        original_mtime = os.stat(visible).st_mtime_ns

        result = codex_session_provider.migrate_current_provider(
            environ=self.environ, data_root=self.data_root)

        self.assertTrue(result['ok'])
        self.assertTrue(result['changed'])
        self.assertEqual(1, result['updated_threads'])
        self.assertEqual(1, result['updated_rollouts'])
        self.assertEqual('codex_local_access', self._first_provider(visible))
        self.assertEqual('codex_local_access',
                         self._db_provider(db_path, 'visible-1'))
        self.assertEqual('openai', self._first_provider(archived))
        self.assertEqual('openai', self._db_provider(db_path, 'archived-1'))
        self.assertEqual('openai', self._first_provider(subagent))
        self.assertEqual('openai', self._db_provider(db_path, 'subagent-1'))
        self.assertEqual(original_mtime, os.stat(visible).st_mtime_ns)
        with open(visible, 'r', encoding='utf-8') as stream:
            content = stream.read()
        self.assertIn(chr(0x1F1E8) + chr(0x1F1F3), content)
        self.assertTrue(os.path.isfile(os.path.join(
            result['backup_dir'], 'manifest.json')))

        repeated = codex_session_provider.migrate_current_provider(
            environ=self.environ, data_root=self.data_root)
        self.assertTrue(repeated['ok'])
        self.assertFalse(repeated['changed'])

        self._write_config('openai')
        restored = codex_session_provider.migrate_current_provider(
            environ=self.environ, data_root=self.data_root)
        self.assertTrue(restored['ok'])
        self.assertEqual(1, restored['updated_threads'])
        self.assertEqual('openai', self._first_provider(visible))
        self.assertEqual('openai', self._db_provider(db_path, 'visible-1'))

    def test_skips_rollout_outside_sessions_without_changing_sqlite(self):
        self._write_config('codex_local_access')
        outside = os.path.join(self.temp.name, 'rollout-outside.jsonl')
        with open(outside, 'w', encoding='utf-8') as stream:
            stream.write(
                '{"type":"session_meta","payload":{"id":"outside",'
                '"model_provider":"openai"}}\n')
        db_path = self._database([
            ('outside', outside, 'openai', 0, 'hello', 'hello', 'cli', 'user'),
        ])

        result = codex_session_provider.migrate_current_provider(
            environ=self.environ, data_root=self.data_root)

        self.assertTrue(result['ok'])
        self.assertFalse(result['changed'])
        self.assertEqual(1, result['skipped_threads'])
        self.assertEqual('openai', self._db_provider(db_path, 'outside'))
        self.assertEqual('openai', self._first_provider(outside))

    def test_refuses_active_profile_without_changing_data(self):
        with open(os.path.join(self.codex_root, 'config.toml'), 'w',
                  encoding='utf-8', newline='\n') as stream:
            stream.write('profile = "work"\nmodel_provider = "codex_local_access"\n')
        rollout = self._rollout('profile-thread', 'openai')
        db_path = self._database([
            ('profile-thread', os.path.relpath(rollout, self.codex_root),
             'openai', 0, 'hello', 'hello', 'cli', 'user'),
        ])

        result = codex_session_provider.migrate_current_provider(
            environ=self.environ, data_root=self.data_root)

        self.assertFalse(result['ok'])
        self.assertIn('活动 profile', result['warning'])
        self.assertEqual('openai', self._first_provider(rollout))
        self.assertEqual('openai', self._db_provider(db_path, 'profile-thread'))

    def test_skips_non_utf8_rollout_without_changing_sqlite(self):
        self._write_config('codex_local_access')
        folder = os.path.join(self.codex_root, 'sessions', '2026', '09', '17')
        os.makedirs(folder, exist_ok=True)
        rollout = os.path.join(folder, 'rollout-invalid-utf8.jsonl')
        with open(rollout, 'wb') as stream:
            stream.write(b'{"type":"session_meta","payload":{"id":"bad-utf8",')
            stream.write(b'"model_provider":"openai","note":"\xff"}}\n')
        db_path = self._database([
            ('bad-utf8', os.path.relpath(rollout, self.codex_root),
             'openai', 0, 'hello', 'hello', 'cli', 'user'),
        ])

        result = codex_session_provider.migrate_current_provider(
            environ=self.environ, data_root=self.data_root)

        self.assertTrue(result['ok'])
        self.assertFalse(result['changed'])
        self.assertEqual(1, result['skipped_threads'])
        self.assertEqual('openai', self._db_provider(db_path, 'bad-utf8'))
        with open(rollout, 'rb') as stream:
            self.assertIn(b'\xff', stream.read())

    def test_skips_mismatched_thread_id_without_changing_sqlite(self):
        self._write_config('codex_local_access')
        rollout = self._rollout('rollout-thread', 'openai')
        db_path = self._database([
            ('sqlite-thread', os.path.relpath(rollout, self.codex_root),
             'openai', 0, 'hello', 'hello', 'cli', 'user'),
        ])

        result = codex_session_provider.migrate_current_provider(
            environ=self.environ, data_root=self.data_root)

        self.assertTrue(result['ok'])
        self.assertFalse(result['changed'])
        self.assertEqual(1, result['skipped_threads'])
        self.assertEqual('openai', self._first_provider(rollout))
        self.assertEqual('openai', self._db_provider(db_path, 'sqlite-thread'))

    def test_missing_config_defaults_target_provider_to_openai(self):
        rollout = self._rollout('default-provider', 'codex_local_access')
        db_path = self._database([
            ('default-provider', os.path.relpath(rollout, self.codex_root),
             'codex_local_access', 0, 'hello', 'hello', 'cli', 'user'),
        ])

        result = codex_session_provider.migrate_current_provider(
            environ=self.environ, data_root=self.data_root)

        self.assertTrue(result['ok'])
        self.assertEqual('openai', result['target_provider'])
        self.assertEqual('openai', self._first_provider(rollout))
        self.assertEqual('openai', self._db_provider(db_path, 'default-provider'))

    def test_skips_database_without_visibility_or_archive_columns(self):
        self._write_config('codex_local_access')
        rollout = self._rollout('legacy-schema', 'openai')
        db_path = os.path.join(self.codex_root, 'state_5.sqlite')
        connection = sqlite3.connect(db_path)
        connection.execute(
            'CREATE TABLE threads ('
            'id TEXT PRIMARY KEY, rollout_path TEXT, model_provider TEXT)')
        connection.execute(
            'INSERT INTO threads VALUES (?, ?, ?)',
            ('legacy-schema', os.path.relpath(rollout, self.codex_root),
             'openai'))
        connection.commit()
        connection.close()

        result = codex_session_provider.migrate_current_provider(
            environ=self.environ, data_root=self.data_root)

        self.assertTrue(result['ok'])
        self.assertFalse(result['changed'])
        self.assertEqual('openai', self._first_provider(rollout))
        self.assertEqual('openai', self._db_provider(db_path, 'legacy-schema'))

    def test_rolls_back_rollout_when_sqlite_update_fails(self):
        self._write_config('codex_local_access')
        rollout = self._rollout('thread-1', 'openai')
        db_path = self._database([
            ('thread-1', os.path.relpath(rollout, self.codex_root),
             'openai', 0, 'hello', 'hello', 'cli', 'user'),
        ])
        with mock.patch.object(
                codex_session_provider, '_update_database',
                side_effect=RuntimeError('injected write failure')):
            result = codex_session_provider.migrate_current_provider(
                environ=self.environ, data_root=self.data_root)

        self.assertFalse(result['ok'])
        self.assertIn('已自动回滚', result['warning'])
        self.assertEqual('openai', self._first_provider(rollout))
        self.assertEqual('openai', self._db_provider(db_path, 'thread-1'))


if __name__ == '__main__':
    unittest.main()
