# -*- coding: utf-8 -*-
import os
import tempfile
import unittest

import bottle

from core import ui_cache


class NoCachePatchTests(unittest.TestCase):
    def setUp(self):
        self.original_static_file = bottle.static_file
        self.original_patched = ui_cache._no_cache_patched
        bottle.static_file = self.original_static_file
        ui_cache._no_cache_patched = False
        self.temp = tempfile.TemporaryDirectory(prefix='cxvpn-uicache-')
        self.asset = os.path.join(self.temp.name, 'index.html')
        with open(self.asset, 'w', encoding='utf-8') as stream:
            stream.write('<html>ui</html>')

    def tearDown(self):
        bottle.static_file = self.original_static_file
        ui_cache._no_cache_patched = self.original_patched
        self.temp.cleanup()

    def test_patch_adds_no_cache_headers(self):
        ui_cache.apply_no_cache_patch()

        response = bottle.static_file('index.html', root=self.temp.name)

        self.assertEqual(
            'no-cache, no-store, must-revalidate',
            response.headers.get('Cache-Control'))
        self.assertEqual('no-cache', response.headers.get('Pragma'))

    def test_patch_is_idempotent(self):
        ui_cache.apply_no_cache_patch()
        first = bottle.static_file
        ui_cache.apply_no_cache_patch()

        self.assertIs(first, bottle.static_file)
        self.assertTrue(ui_cache.is_no_cache_patch_applied())

    def test_patch_preserves_file_content(self):
        ui_cache.apply_no_cache_patch()

        response = bottle.static_file('index.html', root=self.temp.name)

        self.assertIn(b'<html>ui</html>', response.body.read())

    def test_patch_missing_file_still_returns_response(self):
        ui_cache.apply_no_cache_patch()

        response = bottle.static_file('missing.html', root=self.temp.name)

        self.assertEqual(404, response.status_code)


class PurgeStaleCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cxvpn-purge-')
        self.storage = os.path.join(self.temp.name, 'webview_data')

    def tearDown(self):
        self.temp.cleanup()

    def build_storage(self):
        cache = os.path.join(self.storage, 'EBWebView', 'Default', 'Cache')
        code_cache = os.path.join(
            self.storage, 'EBWebView', 'Default', 'Code Cache')
        cookies = os.path.join(self.storage, 'EBWebView', 'Default', 'Cookies')
        for directory in (cache, code_cache):
            os.makedirs(directory, exist_ok=True)
            with open(
                    os.path.join(directory, 'entry'), 'w',
                    encoding='utf-8') as stream:
                stream.write('stale')
        with open(cookies, 'w', encoding='utf-8') as stream:
            stream.write('session')

    def marker(self):
        return os.path.join(self.storage, ui_cache.CACHE_MARKER_NAME)

    def test_version_change_removes_http_cache_only(self):
        self.build_storage()
        with open(self.marker(), 'w', encoding='utf-8') as stream:
            stream.write('1.0.0')

        result = ui_cache.purge_stale_webview_cache(self.storage, '1.1.0')

        self.assertTrue(result['purged'])
        self.assertEqual('version-changed', result['reason'])
        self.assertEqual('1.0.0', result['previous'])
        self.assertFalse(os.path.isdir(
            os.path.join(self.storage, 'EBWebView', 'Default', 'Cache')))
        self.assertFalse(os.path.isdir(
            os.path.join(self.storage, 'EBWebView', 'Default', 'Code Cache')))
        self.assertTrue(os.path.isfile(
            os.path.join(self.storage, 'EBWebView', 'Default', 'Cookies')))
        with open(self.marker(), encoding='utf-8') as stream:
            self.assertEqual('1.1.0', stream.read())

    def test_same_version_keeps_cache(self):
        self.build_storage()
        with open(self.marker(), 'w', encoding='utf-8') as stream:
            stream.write('1.1.0')

        result = ui_cache.purge_stale_webview_cache(self.storage, '1.1.0')

        self.assertFalse(result['purged'])
        self.assertEqual('same-version', result['reason'])
        self.assertTrue(os.path.isdir(
            os.path.join(self.storage, 'EBWebView', 'Default', 'Cache')))

    def test_first_run_purges_and_writes_marker(self):
        self.build_storage()

        result = ui_cache.purge_stale_webview_cache(self.storage, '1.2.2')

        self.assertTrue(result['purged'])
        self.assertEqual('first-run', result['reason'])
        self.assertFalse(os.path.isdir(
            os.path.join(self.storage, 'EBWebView', 'Default', 'Cache')))
        with open(self.marker(), encoding='utf-8') as stream:
            self.assertEqual('1.2.2', stream.read())

    def test_empty_storage_path_is_noop(self):
        result = ui_cache.purge_stale_webview_cache('', '1.2.2')

        self.assertFalse(result['purged'])
        self.assertEqual('no-storage-path', result['reason'])

    def test_log_callback_receives_summary(self):
        self.build_storage()
        entries = []

        ui_cache.purge_stale_webview_cache(
            self.storage, '1.2.2', log=entries.append)

        self.assertEqual(1, len(entries))
        self.assertIn('webview cache purge', entries[0])

    def test_missing_cache_dirs_still_write_marker(self):
        os.makedirs(self.storage, exist_ok=True)

        result = ui_cache.purge_stale_webview_cache(self.storage, '1.2.2')

        self.assertTrue(result['purged'])
        self.assertEqual([], result['removed'])
        self.assertTrue(os.path.isfile(self.marker()))


if __name__ == '__main__':
    unittest.main()
