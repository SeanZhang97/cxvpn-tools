# -*- coding: utf-8 -*-
"""_ps 的 stderr CLIXML 清洗与进度抑制回归。

夹具来自本机 Windows PowerShell 5.1 重定向 stderr 时的真实输出：
Set-VpnConnection 成功/失败都会把进度、错误记录序列化为 CLIXML。
"""
import base64
import subprocess
import unittest
from unittest.mock import patch

from core import vpn_os

_PROGRESS_ONLY = (
    '#< CLIXML\n'
    '<Objs Version="1.1.0.1" xmlns="http://schemas.microsoft.com/powershell'
    '/2004/04"><Obj S="progress" RefId="0"><TN RefId="0"><T>System.Manage'
    'ment.Automation.PSCustomObject</T><T>System.Object</T></TN><MS><I64 '
    'N="SourceId">1</I64><PR N="Record"><AV>???????????????</AV><AI>0</AI'
    '><Nil /><PI>-1</PI><PC>-1</PC><T>Completed</T><SR>-1</SR><SD> </SD>'
    '</PR></MS></Obj>'
)

_ERROR_CLIXML = (
    '#< CLIXML\n'
    '<Objs Version="1.1.0.1" xmlns="http://schemas.microsoft.com/powershell'
    '/2004/04"><Obj S="progress" RefId="0"><TN RefId="0"><T>System.Manage'
    'ment.Automation.PSCustomObject</T></TN><MS><I64 N="SourceId">1</I64>'
    '</MS></Obj>'
    '<S S="Error">Set-VpnConnection :  The configuration cannot be applied'
    ' to the local user VPN connection test-vpn. : The system c'
    '_x000D__x000A_</S>'
    '<S S="Error">ould not find the phone book entry for this connection.'
    '_x000D__x000A_</S>'
    '<S S="Error">?????? ??:1 ???: 41_x000D__x000A_</S>'
    '<S S="Error">+ Set-VpnConnection -Name \'test-vpn\' ...'
    '_x000D__x000A_</S>'
    '<S S="Error">    + CategoryInfo          : ObjectNotFound: '
    '(test-vpn) [Set-VpnConnection], CimException_x000D__x000A_</S>'
    '<S S="Error"> _x000D__x000A_</S>'
    '</Objs>'
)


class CleanStderrTests(unittest.TestCase):
    def test_plain_stderr_passes_through(self):
        self.assertEqual(vpn_os._clean_stderr(' 一些错误 \n'), '一些错误')

    def test_empty_stderr_stays_empty(self):
        self.assertEqual(vpn_os._clean_stderr(''), '')
        self.assertEqual(vpn_os._clean_stderr(None), '')

    def test_progress_only_clixml_is_dropped(self):
        self.assertEqual(vpn_os._clean_stderr(_PROGRESS_ONLY), '')

    def test_error_clixml_keeps_message_and_drops_noise(self):
        cleaned = vpn_os._clean_stderr(_ERROR_CLIXML)
        self.assertTrue(cleaned.startswith(
            'Set-VpnConnection :  The configuration cannot be applied'))
        self.assertIn('ould not find the phone book entry', cleaned)
        self.assertNotIn('CategoryInfo', cleaned)
        self.assertNotIn('+ Set-VpnConnection', cleaned)
        self.assertNotIn('??:1', cleaned)

    def test_error_text_keeps_unicode_and_unescapes_entities(self):
        sample = (
            '#< CLIXML\n<Objs><S S="Error">名称 \'孙河机房\U0001F1E8\U0001F1F3\' '
            '已存在 &lt;重复&gt;_x000D__x000A_</S></Objs>'
        )
        cleaned = vpn_os._clean_stderr(sample)
        self.assertEqual(
            cleaned, "名称 '孙河机房\U0001F1E8\U0001F1F3' 已存在 <重复>")


class PsInvocationTests(unittest.TestCase):
    @patch('core.vpn_os.subprocess.run')
    def test_progress_stream_is_silenced_and_stderr_cleaned(self, run):
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='', stderr=_PROGRESS_ONLY)

        ok, out, err = vpn_os._ps("Set-VpnConnection -Name '办公 VPN'")

        self.assertTrue(ok)
        self.assertEqual(out, '')
        self.assertEqual(err, '')
        args = run.call_args.args[0]
        encoded = args[args.index('-EncodedCommand') + 1]
        command = base64.b64decode(encoded).decode('utf-16le')
        self.assertIn("$ProgressPreference = 'SilentlyContinue'", command)

    @patch('core.vpn_os.subprocess.run')
    def test_failed_command_returns_readable_error(self, run):
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout='', stderr=_ERROR_CLIXML)

        ok, _, err = vpn_os._ps("Set-VpnConnection -Name 'x'")

        self.assertFalse(ok)
        self.assertIn('Set-VpnConnection', err)
        self.assertNotIn('CLIXML', err)


if __name__ == '__main__':
    unittest.main()
