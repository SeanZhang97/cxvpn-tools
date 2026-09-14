# -*- coding: utf-8 -*-
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from core import app_paths, desktop_runtime


class DesktopRuntimeTests(unittest.TestCase):
    def test_actual_path_check_cleans_probe_file(self):
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(app_paths, 'user_data_root', return_value=folder):
            self.assertFalse(desktop_runtime.user_data_is_redirected())
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_new_files_redirected_to_another_directory_are_detected(self):
        resolve = Path.resolve
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(app_paths, 'user_data_root', return_value=folder):
            def resolved(path):
                return (Path(folder) / 'shadow' / path.name if path.name.startswith('.cxvpn-path-')
                        else resolve(path))
            with mock.patch.object(Path, 'resolve', autospec=True, side_effect=resolved):
                self.assertTrue(desktop_runtime.user_data_is_redirected())
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_normal_desktop_does_not_relaunch(self):
        with mock.patch.object(desktop_runtime, 'user_data_is_redirected', return_value=False), \
                mock.patch.object(desktop_runtime, '_shell_launch') as launch:
            desktop_runtime.ensure_desktop_runtime()
            launch.assert_not_called()

    def test_broker_request_preserves_paths_but_not_secret_environment(self):
        seen = []
        def launch(command):
            request = Path(command[-1])
            seen.append(json.loads(request.read_text(encoding='utf-8')))
            desktop_runtime._write_json(request.with_name('response.json'),
                                        {'phase': 'done', 'exit_code': 0})
        with mock.patch.object(desktop_runtime, 'user_data_is_redirected', return_value=True), \
                mock.patch.object(desktop_runtime, '_shell_launch', side_effect=launch), \
                mock.patch.dict(os.environ, {'CODEX_HOME': 'C:/用户/é', 'API_TOKEN': 'offline-only'}):
            with self.assertRaises(SystemExit) as result:
                desktop_runtime.ensure_desktop_runtime(wait=True)
        self.assertEqual(result.exception.code, 0)
        self.assertEqual(seen[0]['environment']['CODEX_HOME'], 'C:/用户/é')
        self.assertNotIn('API_TOKEN', seen[0]['environment'])

    def test_unclaimed_request_times_out_without_reading_configuration(self):
        with mock.patch.object(desktop_runtime, 'user_data_is_redirected', return_value=True), \
                mock.patch.object(desktop_runtime, '_shell_launch'), \
                mock.patch.object(desktop_runtime.time, 'monotonic', side_effect=[0, 16]):
            with self.assertRaises(TimeoutError):
                desktop_runtime.ensure_desktop_runtime()

    def test_broker_refuses_to_launch_while_still_redirected(self):
        with tempfile.TemporaryDirectory(prefix='cxvpn-desktop-') as folder:
            request = Path(folder, 'request.json')
            desktop_runtime._write_json(request, {'command': ['offline'], 'wait': False})
            with mock.patch.object(desktop_runtime, 'user_data_is_redirected', return_value=True), \
                    mock.patch.object(desktop_runtime.subprocess, 'Popen') as launch:
                desktop_runtime._broker_job(str(request))
            self.assertEqual(json.loads(Path(folder, 'response.json').read_text(encoding='utf-8'))['phase'],
                             'failed')
            launch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
