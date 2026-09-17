# -*- coding: utf-8 -*-
"""api.Api 软件升级桥接的离线回归：任务守卫、终态回写与监控超时。"""
import os
import threading
import unittest
from unittest import mock

import api
from core import app_update


def _build_target():
    target = api.Api.__new__(api.Api)
    target._app_download = app_update.UpdateDownloadState()
    target._app_download_cancel = threading.Event()
    target._app_update_lock = threading.Lock()
    target.log = mock.Mock()
    target._poke_ui_state = mock.Mock()
    return target


class StartDownloadTests(unittest.TestCase):
    def test_start_rejects_while_downloading_or_installing(self):
        target = _build_target()
        target._app_download.begin('1.2.0')
        result = target.start_app_update_download(
            'https://github.com/x/y.exe', '1.2.0')
        self.assertFalse(result['ok'])

        target._app_download.update(phase='installing')
        result = target.start_app_update_download(
            'https://github.com/x/y.exe', '1.2.0')
        self.assertFalse(result['ok'])

    def test_start_submits_background_task(self):
        target = _build_target()
        with mock.patch('threading.Thread') as thread_cls:
            thread_cls.return_value = mock.Mock()
            result = target.start_app_update_download(
                'https://github.com/x/y.exe', '1.2.0', 'A' * 64)
        self.assertTrue(result['ok'])
        self.assertEqual(target._app_download.snapshot()['phase'],
                         'downloading')
        thread_cls.assert_called_once()
        kwargs = thread_cls.call_args.kwargs
        self.assertEqual(kwargs.get('name'), 'app-update-download')
        thread_cls.return_value.start.assert_called_once()
        target.log.assert_called_once()

    def test_start_accepts_when_package_already_downloaded(self):
        target = _build_target()
        target._app_download.update(phase='downloaded', path='x.exe',
                                    version='1.2.0')
        result = target.start_app_update_download(
            'https://github.com/x/y.exe', '1.2.0')
        self.assertTrue(result['ok'])


class CancelDownloadTests(unittest.TestCase):
    def test_cancel_signals_event_during_download(self):
        target = _build_target()
        target._app_download.begin('1.2.0')
        result = target.cancel_app_update_download()
        self.assertTrue(result['ok'])
        self.assertTrue(target._app_download_cancel.is_set())

    def test_cancel_rejects_when_idle(self):
        target = _build_target()
        result = target.cancel_app_update_download()
        self.assertFalse(result['ok'])


class RunDownloadTests(unittest.TestCase):
    def test_failure_marks_state_failed_and_pokes_ui(self):
        target = _build_target()
        with mock.patch.object(
                app_update, 'download_installer',
                return_value={'ok': False, 'msg': '下载升级包失败：URLError'}) \
                as download:
            target._run_app_download('https://github.com/x/y.exe', '1.2.0', '')
        download.assert_called_once()
        snapshot = target._app_download.snapshot()
        self.assertEqual(snapshot['phase'], 'failed')
        self.assertIn('URLError', snapshot['msg'])
        target._poke_ui_state.assert_called()

    def test_cancellation_marks_state_cancelled(self):
        target = _build_target()
        with mock.patch.object(
                app_update, 'download_installer',
                return_value={'ok': False, 'cancelled': True,
                              'msg': '已取消下载'}):
            target._run_app_download('https://github.com/x/y.exe', '1.2.0', '')
        snapshot = target._app_download.snapshot()
        self.assertEqual(snapshot['phase'], 'cancelled')
        self.assertEqual(snapshot['progress'], 0)

    def test_success_prunes_old_installers(self):
        target = _build_target()
        target._app_download.begin('1.2.0')
        with mock.patch.object(
                app_update, 'download_installer',
                return_value={'ok': True, 'path': r'D:\new.exe'}) as download, \
                mock.patch.object(app_update, 'prune_old_installers') as prune:
            target._run_app_download('https://github.com/x/y.exe', '1.2.0', '')
        download.assert_called_once()
        prune.assert_called_once_with(r'D:\new.exe')
        target._poke_ui_state.assert_called()


class ApplyUpdateTests(unittest.TestCase):
    def test_apply_rejects_when_package_not_ready(self):
        target = _build_target()
        result = target.apply_app_update()
        self.assertFalse(result['ok'])

        target._app_download.begin('1.2.0')
        result = target.apply_app_update()
        self.assertFalse(result['ok'])

    def test_apply_launches_installer_and_monitor(self):
        target = _build_target()
        target._app_download.begin('1.2.0')
        target._app_download.update(
            phase='downloaded', path=r'D:\pkg.exe', version='1.2.0')
        with mock.patch.object(
                app_update, 'launch_installer',
                return_value={'ok': True, 'msg': '升级已开始'}) as launch, \
                mock.patch('threading.Thread') as thread_cls, \
                mock.patch('sys.executable', r'C:\app\CXVPNTools.exe'):
            thread_cls.return_value = mock.Mock()
            result = target.apply_app_update()
        self.assertTrue(result['ok'])
        self.assertEqual(target._app_download.snapshot()['phase'],
                         'installing')
        launch.assert_called_once()
        args, kwargs = launch.call_args
        self.assertEqual(args[0], r'D:\pkg.exe')
        self.assertEqual(args[1], '1.2.0')
        self.assertEqual(kwargs.get('fallback_dir'), r'C:\app')
        thread_cls.assert_called_once()
        monitor = [call for call in thread_cls.call_args_list
                   if call.kwargs.get('name') == 'app-update-install-monitor']
        self.assertEqual(len(monitor), 1)

    def test_apply_failure_marks_state_failed(self):
        target = _build_target()
        target._app_download.begin('1.2.0')
        target._app_download.update(
            phase='downloaded', path=r'D:\missing.exe', version='1.2.0')
        with mock.patch.object(
                app_update, 'launch_installer',
                return_value={'ok': False, 'msg': '升级包文件不存在，请重新下载'}):
            result = target.apply_app_update()
        self.assertFalse(result['ok'])
        snapshot = target._app_download.snapshot()
        self.assertEqual(snapshot['phase'], 'failed')
        self.assertIn('升级包文件不存在', snapshot['msg'])
        target._poke_ui_state.assert_called()


class InstallMonitorTests(unittest.TestCase):
    def test_monitor_timeout_restores_retryable_state(self):
        target = _build_target()
        target._app_download.begin('1.2.0')
        target._app_download.update(phase='installing')
        with mock.patch.object(app_update, 'installed_display_version',
                               return_value=''):
            target._monitor_app_update_install('1.2.0', timeout=0)
        snapshot = target._app_download.snapshot()
        self.assertEqual(snapshot['phase'], 'failed')
        self.assertIn('升级未完成', snapshot['msg'])
        target._poke_ui_state.assert_called()

    def test_monitor_success_keeps_installing_state(self):
        target = _build_target()
        target._app_download.begin('1.2.0')
        target._app_download.update(phase='installing')
        with mock.patch.object(app_update, 'read_install_result',
                               return_value={'version': '1.2.0', 'phase': 'completed'}), \
                mock.patch('time.sleep') as sleep:
            target._monitor_app_update_install('1.2.0', timeout=10)
        sleep.assert_called_once_with(3)
        self.assertEqual(target._app_download.snapshot()['phase'],
                         'installing')
        target._poke_ui_state.assert_not_called()


class SnapshotBridgeTests(unittest.TestCase):
    def test_build_ui_snapshot_includes_download_state(self):
        target = _build_target()
        target._lock = threading.Lock()
        target._log_version = 3
        target._ip_info_lock = threading.Lock()
        target._ip_info = {'loading': False}
        target._manual = None
        target._sms_ui = False
        target._sms_ui_id = 0
        target.get_state = mock.Mock(return_value={})
        target.vpn_status = mock.Mock(return_value={})
        target.get_browser = mock.Mock(return_value={})
        target.routing_telemetry = mock.Mock()
        target.routing_telemetry.snapshot.return_value = {}
        target._app_download.begin('1.2.0')
        target._app_download.update(progress=42,
                                    downloaded_bytes=2048, total_bytes=4096)

        snapshot = target._build_ui_snapshot()

        self.assertEqual(snapshot['app_update']['phase'], 'downloading')
        self.assertEqual(snapshot['app_update']['progress'], 42)
        self.assertEqual(snapshot['app_update']['version'], '1.2.0')

    def test_build_ui_snapshot_survives_missing_container(self):
        target = _build_target()
        del target._app_download
        target._lock = threading.Lock()
        target._log_version = 1
        target._ip_info_lock = threading.Lock()
        target._ip_info = {}
        target._manual = None
        target._sms_ui = False
        target._sms_ui_id = 0
        target.get_state = mock.Mock(return_value={})
        target.vpn_status = mock.Mock(return_value={})
        target.get_browser = mock.Mock(return_value={})
        target.routing_telemetry = mock.Mock()
        target.routing_telemetry.snapshot.return_value = {}

        snapshot = target._build_ui_snapshot()
        self.assertEqual(snapshot['app_update'], {'phase': 'idle'})


if __name__ == '__main__':
    unittest.main()
