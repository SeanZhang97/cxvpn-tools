# -*- coding: utf-8 -*-
"""发行构建的纯离线测试，不启动 Nuitka、安装器或产品进程。"""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
import zipfile

import build_nuitka_cache as cache
import build_release


class NuitkaCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'sample.py'
        self.source.write_text('value = 1\n', encoding='utf-8')
        self.identity = {'python': 'test', 'abi': 'cp-test', 'nuitka': 'test'}
        self.calls = 0

    def compile(self, command, **kwargs):
        self.calls += 1
        self.assertTrue(kwargs['check'])
        self.assertEqual(kwargs['timeout'], cache.COMPILE_TIMEOUT)
        output = Path(next(arg.split('=', 1)[1] for arg in command
                           if arg.startswith('--output-dir=')))
        (output / 'sample.cp-test.pyd').write_bytes(self.source.read_bytes())

    def run_compile(self, **kwargs):
        return cache.compile_cached(self.source, 'core.sample', self.root / 'cache',
                                    kwargs.get('identity', self.identity),
                                    runner=kwargs.get('runner', self.compile))

    def test_content_hit_and_source_invalidation(self):
        first = self.run_compile()
        self.assertEqual(self.run_compile(), first)
        self.assertEqual(self.calls, 1)
        self.source.write_text('value = 2\n', encoding='utf-8')
        self.assertNotEqual(self.run_compile(), first)
        self.assertEqual(self.calls, 2)

    def test_abi_and_tool_version_invalidate(self):
        first = self.run_compile()
        self.assertNotEqual(first, self.run_compile(identity={'abi': 'new'}))
        self.assertNotEqual(first, self.run_compile(identity={'nuitka': 'new'}))

    def test_package_init_invalidation(self):
        (self.root / '__init__.py').write_text('x = 1', encoding='utf-8')
        first = self.run_compile()
        (self.root / '__init__.py').write_text('x = 2', encoding='utf-8')
        self.assertNotEqual(self.run_compile(), first)

    def test_corrupt_or_missing_completion_rebuilds(self):
        first = self.run_compile()
        first.write_bytes(b'corrupt')
        self.run_compile()
        (first.parent / 'complete.json').unlink()
        self.run_compile()
        self.assertEqual(self.calls, 3)

    def test_failure_never_publishes_complete_marker(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_compile(runner=mock.Mock(side_effect=subprocess.CalledProcessError(1, 'nuitka')))
        self.assertEqual(list((self.root / 'cache').rglob('complete.json')), [])

    def test_no_or_multiple_pyd_fail(self):
        with self.assertRaises(RuntimeError):
            self.run_compile(runner=lambda *args, **kwargs: None)

        def multiple(command, **kwargs):
            self.compile(command, **kwargs)
            output = Path(next(arg.split('=', 1)[1] for arg in command
                               if arg.startswith('--output-dir=')))
            (output / 'core.sample.cp-other.pyd').write_bytes(b'other')

        with self.assertRaises(RuntimeError):
            self.run_compile(runner=multiple)

    def test_cached_path_cannot_escape(self):
        folder = self.root / 'cache'
        folder.mkdir()
        (folder / 'complete.json').write_text(
            json.dumps({'name': '../outside.pyd', 'sha256': 'bad'}), encoding='utf-8')
        self.assertIsNone(cache.read_cached(folder))


class ReleaseArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.payload = self.root / 'CXVPNTools'
        (self.payload / '_internal').mkdir(parents=True)
        (self.payload / 'CXVPNTools.exe').write_bytes(b'fixture')

    def test_unicode_paths_roundtrip(self):
        name = '中文-e\u0301-\U0001f1e8\U0001f1f3.txt'
        (self.payload / '_internal' / name).write_text(name, encoding='utf-8')
        target = build_release.create_core_zip(self.payload, self.root / 'release.zip')
        with zipfile.ZipFile(target) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(archive.read('CXVPNTools/_internal/' + name).decode('utf-8'), name)

    def test_user_state_rejected_without_deleting(self):
        state = self.payload / '_internal' / 'state.sqlite3-wal'
        state.write_bytes(b'sensitive fixture')
        with self.assertRaises(RuntimeError):
            build_release.create_core_zip(self.payload, self.root / 'release.zip')
        self.assertTrue(state.exists())
        self.assertFalse((self.root / 'release.zip').exists())

    def test_credentials_and_snapshots_rejected_without_deleting(self):
        for name in ('auth.json', 'AUTH.JSON', 'codex_config_snapshot.json',
                     'codex_auth_snapshot.json', 'CODEX_SESSION_SNAPSHOT.JSON'):
            with self.subTest(name=name):
                path = self.payload / '_internal' / name
                path.write_text('{}', encoding='utf-8')
                try:
                    with self.assertRaisesRegex(RuntimeError, '用户数据'):
                        build_release.payload_files(self.payload)
                    self.assertTrue(path.exists())
                finally:
                    path.unlink()

    def test_backup_directories_rejected_even_when_empty(self):
        for name in ('codex-session-provider-backups', 'recovery-backups', 'RECOVERY-BACKUPS'):
            with self.subTest(name=name):
                directory = self.payload / '_internal' / name
                directory.mkdir()
                try:
                    with self.assertRaisesRegex(RuntimeError, '用户数据'):
                        build_release.payload_files(self.payload)
                    self.assertTrue(directory.is_dir())
                finally:
                    directory.rmdir()

    def test_manifest_matches_assets_and_lock(self):
        (self.root / 'installer').mkdir()
        (self.root / 'dist').mkdir()
        lock = self.root / 'installer' / 'prereqs.lock.json'
        lock.write_text('{}', encoding='utf-8')
        artifact = self.root / 'dist' / 'setup.exe'
        artifact.write_bytes(b'fixture')
        for status in ('', ' M build_release.py\n', '?? new_source.py\n'):
            with self.subTest(status=status), \
                    mock.patch.object(build_release.subprocess, 'run', side_effect=[
                        mock.Mock(stdout='abc123\n'), mock.Mock(stdout=status)]) as git:
                manifest = build_release.write_manifest(self.root, '1.2.3', [artifact], True, False)
            data = json.loads(manifest.read_text(encoding='utf-8'))
            self.assertEqual(data['artifacts'][0]['sha256'], cache.digest_file(artifact))
            self.assertEqual(data['prerequisites_sha256'], cache.digest_file(lock))
            self.assertEqual(data['revision'], 'abc123')
            self.assertIs(data['source_dirty'], bool(status))
            self.assertEqual(git.call_args_list[1].args[0],
                             ['git', 'status', '--porcelain=v1', '--untracked-files=normal'])
        self.assertTrue(artifact.with_name('setup.exe.sha256').is_file())

    def test_distribution_failure_restores_verified_core(self):
        with mock.patch('core.desktop_runtime.ensure_desktop_runtime'), \
                mock.patch.object(build_release.subprocess, 'run'), \
                mock.patch('build_lifecycle.capture_user_config_snapshot',
                           return_value={'root': 'fixture'}) as snapshot, \
                mock.patch('build_lifecycle.stop_running_apps') as stop, \
                mock.patch('build_lifecycle.complete_build') as restore, \
                mock.patch.object(build_release, 'build_distribution',
                                  side_effect=RuntimeError('fixture failure')):
            with self.assertRaisesRegex(RuntimeError, 'fixture failure'):
                build_release.main([])
        stop.assert_called_once()
        snapshot.assert_called_once()
        restore.assert_called_once()
        self.assertEqual(restore.call_args.args[0], {'root': 'fixture'})


if __name__ == '__main__':
    unittest.main()
