# -*- coding: utf-8 -*-
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest import mock

import build_lifecycle


class BuildLifecycleTest(unittest.TestCase):
    def test_config_snapshot_detects_database_or_json_changes(self):
        with tempfile.TemporaryDirectory() as root:
            database = os.path.join(root, 'state.sqlite3')
            with closing(sqlite3.connect(database)) as conn:
                conn.execute(
                    'CREATE TABLE kv_config ('
                    'id INTEGER PRIMARY KEY, revision INTEGER, payload TEXT)')
                conn.execute(
                    'INSERT INTO kv_config VALUES(1,7,?)',
                    (json.dumps({'routing': {'enabled': True}}),))
                conn.commit()
            config_json = os.path.join(root, 'config.json')
            with open(config_json, 'w', encoding='utf-8') as stream:
                stream.write('{"routing":{"enabled":true}}')

            before = build_lifecycle.capture_user_config_snapshot(root)
            build_lifecycle.verify_user_config_unchanged(before)

            with open(config_json, 'w', encoding='utf-8') as stream:
                stream.write('{"routing":{"enabled":false}}')
            with self.assertRaisesRegex(
                    build_lifecycle.BuildLifecycleError, 'config_json_sha256'):
                build_lifecycle.verify_user_config_unchanged(before)

    def test_build_backup_uses_complete_sqlite_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            database = os.path.join(root, 'state.sqlite3')
            with closing(sqlite3.connect(database)) as conn:
                conn.execute(
                    'CREATE TABLE kv_config ('
                    'id INTEGER PRIMARY KEY, revision INTEGER, payload TEXT)')
                conn.execute(
                    'INSERT INTO kv_config VALUES(1,9,?)',
                    (json.dumps({'phone': '13800000000'}),))
                conn.commit()

            backup = build_lifecycle.create_user_state_backup(root)

            self.assertTrue(os.path.isfile(backup))
            with closing(sqlite3.connect(backup)) as conn:
                payload = conn.execute(
                    'SELECT payload FROM kv_config WHERE id=1').fetchone()[0]
            self.assertEqual(json.loads(payload)['phone'], '13800000000')

    @mock.patch('build_lifecycle._powershell')
    def test_stop_running_apps_uses_bounded_exact_process_names(self, run):
        run.return_value = (
            'CXVPN_RUNNING_ROOT=D:\\develop\\CXVPNTools\n'
            'CXVPN_RUNNING_ROOT=D:\\develop\\workspace\\dist\\CXVPNTools\n')
        build_lifecycle.stop_running_apps()

        script = run.call_args.args[0]
        self.assertIn("'CXVPNTools'", script)
        self.assertIn("'CX VPN TOOLS'", script)
        self.assertIn("'CXVPN管理器'", script)
        self.assertIn('AddSeconds(15)', script)
        self.assertEqual(run.call_args.kwargs['timeout'], 20)

    @mock.patch('build_lifecycle._powershell')
    def test_stop_running_apps_returns_exact_running_install_roots(self, run):
        run.return_value = (
            'CXVPN_RUNNING_ROOT=D:\\develop\\CXVPNTools\n'
            'ignored output\n')

        roots = build_lifecycle.stop_running_apps()

        self.assertEqual(
            roots, [os.path.abspath('D:\\develop\\CXVPNTools')])

    @mock.patch('build_lifecycle.time.sleep')
    @mock.patch('build_lifecycle.subprocess.Popen')
    def test_start_packaged_app_confirms_launched_process_stays_alive(
            self, popen, _sleep):
        process = mock.Mock(pid=321)
        process.poll.return_value = None
        popen.return_value = process
        with tempfile.TemporaryDirectory() as root:
            executable = os.path.join(root, 'CXVPNTools.exe')
            with open(executable, 'wb') as stream:
                stream.write(b'exe')
            with mock.patch('build_lifecycle.time.monotonic',
                            side_effect=[0, 0, 6]):
                pid = build_lifecycle.start_packaged_app(root)

        self.assertEqual(pid, 321)
        self.assertEqual(
            os.path.normcase(popen.call_args.args[0][0]),
            os.path.normcase(executable))


if __name__ == '__main__':
    unittest.main()
