# -*- coding: utf-8 -*-
import base64
import subprocess
import unittest
from unittest import mock

from core import codex_runtime


class CodexRuntimeTests(unittest.TestCase):
    def test_restart_targets_only_openai_codex_package_and_waits_for_main_process(self):
        runner = mock.Mock(side_effect=[
            subprocess.CompletedProcess(
                [], 0,
                '{"ok":true,"was_running":true,"graceful_requests":1,'
                '"forced_processes":2}\n', ''),
            subprocess.CompletedProcess(
                [], 0, '{"ok":true,"pid":4321}\n', ''),
        ])
        migrator = mock.Mock(return_value={
            'ok': True, 'changed': True, 'updated_threads': 3,
            'updated_rollouts': 3,
        })

        result = codex_runtime.restart_codex(
            runner=runner, provider_migrator=migrator)

        self.assertTrue(result['ok'])
        self.assertIn('已同步 3 个历史任务', result['msg'])
        self.assertEqual(2, result['forced_processes'])
        self.assertEqual(2, runner.call_count)
        stop_command = runner.call_args_list[0].args[0]
        start_command = runner.call_args_list[1].args[0]
        stop_script = base64.b64decode(
            stop_command[-1]).decode('utf-16-le')
        start_script = base64.b64decode(
            start_command[-1]).decode('utf-16-le')
        self.assertIn("Get-AppxPackage -Name 'OpenAI.Codex'", stop_script)
        self.assertIn("Name = 'ChatGPT.exe'", stop_script)
        self.assertNotIn('shell:AppsFolder', stop_script)
        self.assertIn('shell:AppsFolder', start_script)
        self.assertIn("--type=", start_script)
        self.assertEqual(
            codex_runtime.STOP_TIMEOUT_SECONDS,
            runner.call_args_list[0].kwargs['timeout'])
        self.assertEqual(
            codex_runtime.START_TIMEOUT_SECONDS,
            runner.call_args_list[1].kwargs['timeout'])
        self.assertEqual('utf-8', runner.call_args_list[0].kwargs['encoding'])
        migrator.assert_called_once()

    def test_restart_reports_process_failure_without_retrying(self):
        runner = mock.Mock(return_value=subprocess.CompletedProcess(
            [], 1, '', 'PowerShell failure'))

        result = codex_runtime.restart_codex(runner=runner)

        self.assertFalse(result['ok'])
        self.assertIn('Windows 未能关闭 Codex', result['warning'])
        runner.assert_called_once()

    def test_restart_starts_codex_even_when_provider_migration_fails(self):
        runner = mock.Mock(side_effect=[
            subprocess.CompletedProcess(
                [], 0,
                '{"ok":true,"was_running":true,"forced_processes":0}\n',
                ''),
            subprocess.CompletedProcess(
                [], 0, '{"ok":true,"pid":123}\n', ''),
        ])
        migrator = mock.Mock(return_value={
            'ok': False, 'warning': '历史任务 Provider 同步失败：测试错误',
        })

        result = codex_runtime.restart_codex(
            runner=runner, provider_migrator=migrator)

        self.assertFalse(result['ok'])
        self.assertTrue(result['app_started'])
        self.assertIn('Codex 已重新启动', result['warning'])
        self.assertEqual(2, runner.call_count)

    def test_restart_starts_codex_when_provider_migrator_raises(self):
        runner = mock.Mock(side_effect=[
            subprocess.CompletedProcess(
                [], 0,
                '{"ok":true,"was_running":true,"forced_processes":0}\n',
                ''),
            subprocess.CompletedProcess(
                [], 0, '{"ok":true,"pid":456}\n', ''),
        ])
        migrator = mock.Mock(side_effect=AssertionError('unexpected failure'))

        result = codex_runtime.restart_codex(
            runner=runner, provider_migrator=migrator)

        self.assertFalse(result['ok'])
        self.assertTrue(result['app_started'])
        self.assertIn('Codex 已重新启动', result['warning'])
        self.assertIn('AssertionError', result['warning'])
        self.assertEqual(2, runner.call_count)

    def test_restart_starts_codex_when_migration_result_is_invalid(self):
        runner = mock.Mock(side_effect=[
            subprocess.CompletedProcess(
                [], 0,
                '{"ok":true,"was_running":false,"forced_processes":0}\n',
                ''),
            subprocess.CompletedProcess(
                [], 0, '{"ok":true,"pid":789}\n', ''),
        ])

        result = codex_runtime.restart_codex(
            runner=runner, provider_migrator=mock.Mock(return_value=None))

        self.assertFalse(result['ok'])
        self.assertTrue(result['app_started'])
        self.assertIn('TypeError', result['warning'])
        self.assertEqual(2, runner.call_count)

    def test_restart_has_bounded_timeout(self):
        runner = mock.Mock(side_effect=subprocess.TimeoutExpired('powershell', 35))

        result = codex_runtime.restart_codex(runner=runner)

        self.assertFalse(result['ok'])
        self.assertIn('超时', result['warning'])


if __name__ == '__main__':
    unittest.main()
