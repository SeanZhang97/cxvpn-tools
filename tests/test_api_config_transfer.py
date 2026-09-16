# -*- coding: utf-8 -*-
"""配置文件选择与导入导出的离线测试；不打开窗口或修改真实配置。"""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import api
from core import config_maintenance
from tests.test_config_maintenance import sample_config


class ApiConfigTransferTests(unittest.TestCase):
    def instance(self):
        target = api.Api.__new__(api.Api)
        target.worker = mock.Mock()
        target._cfg_get = mock.Mock(return_value=sample_config())
        target._routing_lock = threading.Lock()
        target._apply_routing_locked = mock.Mock(return_value={'ok': True})
        target.log = mock.Mock()
        return target

    def test_export_uses_save_dialog_and_returns_written_path(self):
        from webview import FileDialog

        target = self.instance()
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / '配置-e\u0301-\U0001f1e8\U0001f1f3.json'
            target.worker.main_window.create_file_dialog.return_value = (str(path),)
            result = target.export_config_backup(True)
            self.assertTrue(result['ok'])
            self.assertEqual(result['path'], str(path))
            self.assertNotIn('content', result)
            self.assertIn('sub.example.test', path.read_text(encoding='utf-8'))
            call = target.worker.main_window.create_file_dialog.call_args
            self.assertEqual(call.args, (FileDialog.SAVE,))
            self.assertFalse(call.kwargs['allow_multiple'])
            self.assertTrue(call.kwargs['save_filename'].endswith('.json'))

    def test_cancelled_export_does_not_write_or_report_success(self):
        target = self.instance()
        target.worker.main_window.create_file_dialog.return_value = None
        with mock.patch('api.config_maintenance.write_backup_file') as write:
            result = target.export_config_backup()
        self.assertEqual(result, {'ok': True, 'cancelled': True})
        write.assert_not_called()
        self.assertFalse(any('导出成功' in call.args[0] for call in target.log.call_args_list))

    def test_write_failure_returns_error_without_success(self):
        target = self.instance()
        target.worker.main_window.create_file_dialog.return_value = ('配置.json',)
        with mock.patch('api.config_maintenance.write_backup_file', side_effect=PermissionError('拒绝写入 token=private-value')):
            result = target.export_config_backup()
        self.assertFalse(result['ok'])
        self.assertIn('拒绝写入', result['msg'])
        self.assertNotIn('path', result)
        self.assertFalse(any('导出成功' in call.args[0] for call in target.log.call_args_list))
        self.assertNotIn('private-value', '\n'.join(call.args[0] for call in target.log.call_args_list))

    def test_import_uses_open_dialog_and_previews_without_applying(self):
        from webview import FileDialog

        target = self.instance()
        content = config_maintenance.create_backup(sample_config())['content']
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / '导入.json'
            path.write_text(content, encoding='utf-8', newline='')
            target.worker.main_window.create_file_dialog.return_value = (str(path),)
            result = target.preview_config_restore()
        self.assertTrue(result['ok'])
        self.assertEqual(result['content'], content)
        self.assertEqual(result['path'], str(path))
        self.assertEqual(result['summary']['provider_count'], 1)
        self.assertEqual(target.worker.main_window.create_file_dialog.call_args.args, (FileDialog.OPEN,))
        target._apply_routing_locked.assert_not_called()

    def test_cancelled_import_does_not_read_or_apply(self):
        target = self.instance()
        target.worker.main_window.create_file_dialog.return_value = None
        with mock.patch('api.config_maintenance.read_backup_file') as read:
            result = target.preview_config_restore()
        self.assertTrue(result['cancelled'])
        read.assert_not_called()
        target._apply_routing_locked.assert_not_called()

    def test_invalid_import_does_not_apply(self):
        for content in ('not-json', '{}'):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as root:
                target = self.instance()
                path = Path(root) / 'invalid.json'
                path.write_text(content, encoding='utf-8')
                target.worker.main_window.create_file_dialog.return_value = (str(path),)
                result = target.preview_config_restore()
                self.assertFalse(result['ok'])
                target._apply_routing_locked.assert_not_called()

    def test_confirmed_import_reuses_transaction_and_reports_failure(self):
        for success in (True, False):
            with self.subTest(success=success):
                target = self.instance()
                target._apply_routing_locked.return_value = {'ok': success, 'msg': '保存失败'}
                content = config_maintenance.create_backup(sample_config())['content']
                result = target.restore_config_backup(content)
                self.assertEqual(result['ok'], success)
                target._apply_routing_locked.assert_called_once()
                self.assertEqual(target._apply_routing_locked.call_args.args[1], 'backup_restore')
                if not success:
                    self.assertEqual(result['msg'], '保存失败')
                    self.assertFalse(any('导入成功' in call.args[0] for call in target.log.call_args_list))

    def test_missing_window_reports_failure(self):
        target = self.instance()
        target.worker.main_window = None
        self.assertFalse(target.export_config_backup()['ok'])
        self.assertFalse(target.preview_config_restore()['ok'])


if __name__ == '__main__':
    unittest.main()
