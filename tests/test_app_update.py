# -*- coding: utf-8 -*-
"""core.app_update 离线回归：检查、下载进度、取消、校验与文案规范。"""
import hashlib
import os
import tempfile
import threading
import unittest
import urllib.error
from unittest import mock

from core import app_update


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self._offset = 0
        self.headers = {'Content-Length': str(len(payload))}

    def read(self, size=-1):
        if self._offset >= len(self._payload):
            return b''
        if size is None or size < 0:
            chunk = self._payload[self._offset:]
        else:
            chunk = self._payload[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _no_forbidden_words(text):
    lowered = str(text or '').lower()
    return 'github' not in lowered and 'release' not in lowered


class CheckLatestTests(unittest.TestCase):
    def test_payload_extracts_installer_and_sha256(self):
        payload = {
            'tag_name': 'v1.3.0',
            'html_url': 'https://example.invalid/notes',
            'assets': [{
                'name': 'CXVPNTools-1.3.0-setup.exe',
                'browser_download_url':
                    'https://github.com/SeanZhang97/cxvpn-tools/'
                    'releases/download/v1.3.0/CXVPNTools-1.3.0-setup.exe',
                'digest': 'sha256:' + 'a' * 64,
            }],
            'body': '更新说明',
            'published_at': '2026-09-01T00:00:00Z',
        }
        with mock.patch.object(app_update, '_request',
                               return_value=b'{}'), \
                mock.patch('json.loads', return_value=payload):
            result = app_update.check_latest()
        self.assertTrue(result['ok'])
        self.assertEqual(result['latest_version'], '1.3.0')
        self.assertTrue(result['newer'])
        self.assertTrue(result['installer_url'].endswith('.exe'))
        self.assertEqual(result['installer_sha256'], 'a' * 64)

    def test_missing_digest_leaves_sha_empty(self):
        payload = {
            'tag_name': 'v1.0.0',
            'assets': [{'name': 'setup.exe',
                        'browser_download_url': 'https://github.com/x/y.exe'}],
        }
        with mock.patch.object(app_update, '_request', return_value=b'{}'), \
                mock.patch('json.loads', return_value=payload):
            result = app_update.check_latest()
        self.assertEqual(result['installer_sha256'], '')

    def test_http_404_reports_no_updates_with_clean_wording(self):
        error = urllib.error.HTTPError(
            app_update.API_URL, 404, 'Not Found', None, None)
        error.close()
        with mock.patch.object(app_update, '_request', side_effect=error):
            result = app_update.check_latest()
        self.assertTrue(result['ok'])
        self.assertFalse(result['newer'])
        self.assertTrue(_no_forbidden_words(result.get('msg')))
        self.assertIn('暂无可用更新', result['msg'])

    def test_http_error_and_network_failure_wording(self):
        error = urllib.error.HTTPError(
            app_update.API_URL, 503, 'Unavailable', None, None)
        error.close()
        with mock.patch.object(app_update, '_request', side_effect=error):
            result = app_update.check_latest()
        self.assertFalse(result['ok'])
        self.assertIn('HTTP 503', result['msg'])
        self.assertTrue(_no_forbidden_words(result['msg']))

        with mock.patch.object(
                app_update, '_request',
                side_effect=urllib.error.URLError('timeout')):
            result = app_update.check_latest()
        self.assertFalse(result['ok'])
        self.assertIn('URLError', result['msg'])
        self.assertTrue(_no_forbidden_words(result['msg']))

    def test_open_release_rejects_untrusted_url(self):
        result = app_update.open_release('https://example.invalid/download')
        self.assertFalse(result['ok'])
        self.assertTrue(_no_forbidden_words(result['msg']))


class UpdateDownloadStateTests(unittest.TestCase):
    def test_begin_resets_fields_to_downloading(self):
        state = app_update.UpdateDownloadState()
        state.update(phase='failed', msg='旧错误', progress=33)
        state.begin('1.2.0')
        snapshot = state.snapshot()
        self.assertEqual(snapshot['phase'], 'downloading')
        self.assertEqual(snapshot['version'], '1.2.0')
        self.assertEqual(snapshot['progress'], 0)
        self.assertEqual(snapshot['msg'], '')
        self.assertGreater(snapshot['started_at'], 0)

    def test_snapshot_is_isolated_copy(self):
        state = app_update.UpdateDownloadState()
        snapshot = state.snapshot()
        snapshot['phase'] = 'hacked'
        self.assertEqual(state.snapshot()['phase'], 'idle')

    def test_concurrent_updates_remain_consistent(self):
        state = app_update.UpdateDownloadState()
        state.begin('1.2.0')

        def worker(index):
            for step in range(50):
                state.update(progress=step, downloaded_bytes=index + step)

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(state.snapshot()['phase'], 'downloading')


class DownloadInstallerTests(unittest.TestCase):
    URL = ('https://github.com/SeanZhang97/cxvpn-tools/releases/'
           'download/v1.2.0/CXVPNTools-1.2.0-setup.exe')

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='cxvpn-update-')
        self.addCleanup(self._tmp.cleanup)
        import core.app_paths
        self._app_paths = core.app_paths
        # app_update 通过 app_paths.user_data_root 定位下载目录。
        self._user_data = os.path.join(self._tmp.name, 'data')
        os.makedirs(self._user_data, exist_ok=True)

    def _patch_data_root(self):
        return mock.patch.object(
            app_update.app_paths, 'user_data_root',
            lambda *args, **kwargs: self._user_data)

    def test_rejects_untrusted_url_without_network(self):
        with mock.patch('urllib.request.urlopen',
                        side_effect=AssertionError('不应发起网络请求')):
            result = app_update.download_installer('https://example.invalid/x.exe')
        self.assertFalse(result['ok'])
        self.assertTrue(_no_forbidden_words(result['msg']))

    def test_successful_download_reports_progress_and_lands_file(self):
        payload = os.urandom(1024 * 512)
        digest = hashlib.sha256(payload).hexdigest()
        state = app_update.UpdateDownloadState()
        state.begin('1.2.0')
        response = _FakeResponse(payload)
        with self._patch_data_root(), \
                mock.patch('urllib.request.urlopen', return_value=response):
            result = app_update.download_installer(
                self.URL, expected_sha256=digest, state=state)
        self.assertTrue(result['ok'])
        target = os.path.join(self._user_data, 'updates',
                              'CXVPNTools-1.2.0-setup.exe')
        self.assertEqual(result['path'], target)
        self.assertTrue(os.path.isfile(target))
        with open(target, 'rb') as stream:
            self.assertEqual(stream.read(), payload)
        snapshot = state.snapshot()
        self.assertEqual(snapshot['phase'], 'downloaded')
        self.assertEqual(snapshot['progress'], 100)
        self.assertEqual(snapshot['total_bytes'], len(payload))
        self.assertEqual(snapshot['path'], target)
        # 临时文件不应残留。
        leftovers = [name for name in os.listdir(os.path.dirname(target))
                     if name.endswith('.tmp')]
        self.assertEqual(leftovers, [])

    def test_sha_mismatch_fails_and_cleans_temporary(self):
        payload = os.urandom(4096)
        state = app_update.UpdateDownloadState()
        response = _FakeResponse(payload)
        with self._patch_data_root(), \
                mock.patch('urllib.request.urlopen', return_value=response):
            result = app_update.download_installer(
                self.URL, expected_sha256='0' * 64, state=state)
        self.assertFalse(result['ok'])
        self.assertIn('下载升级包失败', result['msg'])
        self.assertEqual(state.snapshot()['phase'], 'downloading')
        updates_dir = os.path.join(self._user_data, 'updates')
        self.assertEqual([n for n in os.listdir(updates_dir)
                          if n.endswith('.tmp')], [])
        self.assertFalse(os.path.exists(
            os.path.join(updates_dir, 'CXVPNTools-1.2.0-setup.exe')))

    def test_cancel_event_aborts_download(self):
        payload = os.urandom(64 * 1024)
        state = app_update.UpdateDownloadState()
        cancel = threading.Event()
        cancel.set()
        response = _FakeResponse(payload)
        with self._patch_data_root(), \
                mock.patch('urllib.request.urlopen', return_value=response):
            result = app_update.download_installer(
                self.URL, state=state, cancel_event=cancel)
        self.assertFalse(result['ok'])
        self.assertTrue(result.get('cancelled'))
        self.assertTrue(_no_forbidden_words(result['msg']))
        updates_dir = os.path.join(self._user_data, 'updates')
        self.assertEqual(os.listdir(updates_dir), [])

    def test_transport_failure_returns_typed_error(self):
        state = app_update.UpdateDownloadState()
        with self._patch_data_root(), \
                mock.patch('urllib.request.urlopen',
                           side_effect=urllib.error.URLError('reset')):
            result = app_update.download_installer(self.URL, state=state)
        self.assertFalse(result['ok'])
        self.assertIn('URLError', result['msg'])
        self.assertTrue(_no_forbidden_words(result['msg']))

    def test_progress_updates_track_content_length(self):
        payload = os.urandom(256 * 1024)
        state = app_update.UpdateDownloadState()
        state.begin('1.2.0')
        # 压缩上报间隔，确保多块下载会触发多次进度更新。
        with self._patch_data_root(), \
                mock.patch.object(app_update, 'PROGRESS_REPORT_INTERVAL', 0), \
                mock.patch('urllib.request.urlopen',
                           return_value=_FakeResponse(payload)):
            result = app_update.download_installer(self.URL, state=state)
        self.assertTrue(result['ok'])
        self.assertEqual(state.snapshot()['progress'], 100)
        self.assertGreaterEqual(state.snapshot()['total_bytes'], len(payload))


class HousekeepingTests(unittest.TestCase):
    def test_prune_old_installers_keeps_current_and_scripts(self):
        with tempfile.TemporaryDirectory(prefix='cxvpn-prune-') as base:
            root = os.path.join(base, 'updates')
            os.makedirs(root)
            keep = os.path.join(root, 'CXVPNTools-9.9.9-setup.exe')
            stale = os.path.join(root, 'CXVPNTools-1.0.0-setup.exe')
            script = os.path.join(root, 'apply-update.ps1')
            for path in (keep, stale):
                with open(path, 'wb') as stream:
                    stream.write(b'x')
            with open(script, 'w', encoding='utf-8') as stream:
                stream.write('# guard')
            with mock.patch.object(
                    app_update.app_paths, 'user_data_root',
                    lambda *args, **kwargs: base):
                app_update.prune_old_installers(keep)
            self.assertTrue(os.path.exists(keep))
            self.assertFalse(os.path.exists(stale))
            self.assertTrue(os.path.exists(script))

    def test_installed_display_version_handles_missing_key(self):
        with mock.patch.object(app_update, 'winreg', create=True) as fake:
            fake.OpenKey.side_effect = OSError('not found')
            self.assertEqual(app_update.installed_display_version(), '')


if __name__ == '__main__':
    unittest.main()
