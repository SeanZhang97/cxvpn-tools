import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from core import app_update
from tests.test_app_update import _FakeResponse
from tests.test_api_app_update import _build_target


URL = 'https://github.com/SeanZhang97/cxvpn-tools/releases/download/v9.9.9/CXVPNTools-9.9.9-setup.exe'
SHA = hashlib.sha256(b'fixture').hexdigest()


class HardenedUpdateTests(unittest.TestCase):
    def payload(self):
        return {'tag_name': 'v9.9.9', 'assets': [
            {'name': 'VC_redist.x64.exe', 'browser_download_url': 'https://example.invalid/runtime.exe'},
            {'name': 'CXVPNTools-9.9.9-setup.exe', 'browser_download_url': URL, 'digest': 'sha256:' + SHA},
        ]}

    def check(self, payload, checksum=None):
        results = [json.dumps(payload).encode('utf-8')]
        if checksum is not None:
            results.append(checksum)
        with mock.patch.object(app_update, '_request', side_effect=results):
            return app_update.check_latest()

    def test_exact_installer_selected_not_first_exe(self):
        self.assertEqual(URL, self.check(self.payload())['installer_url'])

    def test_duplicate_or_wrong_url_cannot_be_executed(self):
        data = self.payload()
        data['assets'].append(dict(data['assets'][1]))
        self.assertEqual('', self.check(data)['installer_url'])
        data = self.payload()
        data['assets'][1]['browser_download_url'] = URL.replace('SeanZhang97', 'another-owner')
        self.assertEqual('', self.check(data)['installer_url'])

    def test_missing_digest_uses_exact_checksum_asset(self):
        data = self.payload()
        data['assets'][1].pop('digest')
        data['assets'].append({'name': 'CXVPNTools-9.9.9-setup.exe.sha256', 'browser_download_url': URL + '.sha256'})
        result = self.check(data, (SHA + '  CXVPNTools-9.9.9-setup.exe\n').encode())
        self.assertEqual(SHA, result['installer_sha256'])
        self.assertEqual(URL, result['installer_url'])

    def test_missing_verifiable_digest_disables_auto_install(self):
        data = self.payload()
        data['assets'][1]['digest'] = 'sha256:short'
        self.assertEqual('', self.check(data)['installer_url'])

    def test_dependencies_draft_and_prerelease_are_not_updates(self):
        for field in ('draft', 'prerelease'):
            data = self.payload()
            data[field] = True
            self.assertFalse(self.check(data)['ok'])
        data = self.payload()
        data['tag_name'] = 'prerequisites-2026-09-11-x64'
        self.assertFalse(self.check(data)['ok'])

    def test_untrusted_variations_rejected(self):
        for url in (URL + '?x=1', URL.replace('https:', 'http:'),
                    URL.replace('9.9.9-setup', '9.9.8-setup'), URL.replace('github.com', 'github.com.evil.test')):
            with self.assertRaises(ValueError):
                app_update.installer_identity(url)

    def test_directory_failure_returns_terminal_result(self):
        with mock.patch.object(app_update.os, 'makedirs', side_effect=PermissionError):
            result = app_update.download_installer(URL, SHA)
        self.assertFalse(result['ok'])
        self.assertIn('PermissionError', result['msg'])

    def test_cached_verified_installer_reused(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(app_update, 'updates_root', return_value=folder):
            path = Path(folder, 'CXVPNTools-9.9.9-setup.exe')
            path.write_bytes(b'fixture')
            state = app_update.UpdateDownloadState()
            with mock.patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')):
                result = app_update.download_installer(URL, SHA, state)
            self.assertTrue(result['ok'])
            self.assertEqual(SHA, state.snapshot()['sha256'])

    def test_truncated_response_and_total_timeout_leave_no_exe(self):
        for timeout in (False, True):
            with self.subTest(timeout=timeout), tempfile.TemporaryDirectory() as folder, \
                    mock.patch.object(app_update, 'updates_root', return_value=folder):
                response = _FakeResponse(b'fixture')
                response.headers['Content-Length'] = '100'
                with mock.patch('urllib.request.urlopen', return_value=response), \
                        mock.patch.object(app_update, 'DOWNLOAD_TOTAL_TIMEOUT', -1 if timeout else 900):
                    result = app_update.download_installer(URL, SHA)
                self.assertFalse(result['ok'])
                self.assertEqual([], list(Path(folder).iterdir()))

    def test_guard_write_failure_and_changed_installer_are_safe(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'CXVPNTools-9.9.9-setup.exe')
            path.write_bytes(b'fixture')
            with mock.patch.object(app_update, '_powershell_exe', return_value=str(path)), \
                    mock.patch.object(app_update, '_write_update_guard_script', side_effect=OSError), \
                    mock.patch.object(app_update.subprocess, 'Popen') as popen:
                result = app_update.launch_installer(str(path), '9.9.9', expected_sha256=SHA)
                self.assertFalse(result['ok'])
                popen.assert_not_called()
            with mock.patch.object(app_update.subprocess, 'Popen') as popen:
                result = app_update.launch_installer(str(path), '9.9.9', expected_sha256='0' * 64)
                self.assertFalse(result['ok'])
                popen.assert_not_called()

    def test_api_unexpected_download_error_is_terminal(self):
        target = _build_target()
        target._app_download.begin('9.9.9')
        with mock.patch.object(app_update, 'download_installer', side_effect=RuntimeError):
            target._run_app_download(URL, '9.9.9', SHA)
        self.assertEqual('failed', target._app_download.snapshot()['phase'])

    def test_monitor_uses_process_outcome_and_preserves_attention(self):
        for outcome, expected in (('failed', 'failed'), ('restart_required', 'restart_required'), ('timed_out', 'attention')):
            target = _build_target()
            target._app_download.begin('9.9.9')
            target._app_download.update(phase='installing')
            with mock.patch.object(app_update, 'read_install_result', return_value={'version': '9.9.9', 'phase': outcome}), \
                    mock.patch('time.sleep'):
                target._monitor_app_update_install('9.9.9', timeout=1)
            self.assertEqual(expected, target._app_download.snapshot()['phase'])

    def test_attention_prevents_retry_if_installer_identity_unknown(self):
        target = _build_target()
        target._app_download.update(phase='attention')
        with mock.patch.object(app_update, 'installer_process_running', return_value=None):
            self.assertFalse(target.start_app_update_download(URL, '9.9.9', SHA)['ok'])

    def test_installer_only_auto_restarts_for_update_request(self):
        installer = Path('installer/CXVPNTools.iss').read_text(encoding='utf-8-sig')
        self.assertIn('{param:CXVPNAUTORESTART|0}', installer)
        self.assertIn('Check: ShouldAutoRestartApplication', installer)
        self.assertIn('Result := WizardSilent and CanStartApplication', installer)


@unittest.skipUnless(os.name == 'nt', 'PowerShell Windows fixtures')
class UpdateGuardFixtureTests(unittest.TestCase):
    def test_guard_exit_codes_and_startup_without_real_installers(self):
        wrapper = r'''
param([string]$Guard, [string]$ResultPath, [string]$Scenario)
$ErrorActionPreference = 'Stop'
function Get-FileHash { param($LiteralPath,$Algorithm)
  @{ Hash = $(if ($Scenario -eq 'hash') { 'b' * 64 } else { 'a' * 64 }) }
}
function Get-ItemProperty { param($LiteralPath,$ErrorAction)
  @{DisplayVersion='9.9.9';InstallLocation=$PSScriptRoot} }
function Get-Item { param($LiteralPath)
  @{VersionInfo=@{ProductVersion='9.9.9'}} }
function Test-Path { param($LiteralPath) $true }
function Start-Sleep { param($Seconds) }
function Get-Process { param($Name,$ErrorAction)
  if ($null -ne $script:runningApplication) { return $script:runningApplication }
}
function New-Application([int]$Id) {
  $item = [pscustomobject]@{Id=$Id;Path=(Join-Path $PSScriptRoot 'CXVPNTools.exe')}
  $item | Add-Member ScriptMethod Dispose { }
  return $item
}
function Start-Process { param($FilePath,$ArgumentList,$Verb,$WindowStyle,[switch]$PassThru,$WorkingDirectory)
  if ($Scenario -eq 'uac') { throw 'UAC cancelled' }
  $code = switch ($Scenario) { 'reboot' {3010} 'prepare-reboot' {8} 'fail' {42} default {0} }
  $isInstaller = ($Verb -eq 'RunAs')
  $item = [pscustomobject]@{Id=$(if ($isInstaller) {123} else {456});StartTime=[DateTime]::Now;
    ExitCode=$code;IsInstaller=$isInstaller;Path=$(if ($isInstaller) { $FilePath } else { Join-Path $PSScriptRoot 'CXVPNTools.exe' })}
  $item | Add-Member ScriptMethod WaitForExit {
    param($timeout)
    if ($this.IsInstaller -and $Scenario -eq 'installer-restart') {
      $script:runningApplication = New-Application 789
    }
    if ($this.IsInstaller) { return $Scenario -ne 'timeout' }
    return $Scenario -eq 'early-exit'
  }
  $item | Add-Member ScriptMethod Dispose { }
  if (-not $isInstaller -and $Scenario -ne 'early-exit') {
    $script:runningApplication = New-Application 456
    $item = $script:runningApplication
  }
  return $item
}
& $Guard -TargetVersion '9.9.9' -Installer 'fixture.exe' -ExpectedSHA256 ('a' * 64) -ResultPath $ResultPath
'''
        cases = {'success': 'completed', 'hash': 'failed', 'uac': 'failed',
                 'installer-restart': 'completed',
                 'reboot': 'restart_required', 'prepare-reboot': 'restart_required',
                 'fail': 'failed', 'timeout': 'timed_out', 'early-exit': 'failed'}
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            (base / 'guard.ps1').write_text(app_update._UPDATE_GUARD_PS1, encoding='utf-8-sig')
            (base / 'wrapper.ps1').write_text(wrapper, encoding='utf-8-sig')
            for scenario, expected in cases.items():
                with self.subTest(scenario=scenario):
                    result = base / (scenario + '.json')
                    subprocess.run([app_update._powershell_exe(), '-NoProfile', '-NonInteractive',
                        '-ExecutionPolicy', 'Bypass', '-File', str(base / 'wrapper.ps1'),
                        '-Guard', str(base / 'guard.ps1'), '-ResultPath', str(result), '-Scenario', scenario],
                        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    outcome = json.loads(result.read_text(encoding='utf-8-sig'))
                    self.assertEqual(expected, outcome['phase'])
                    if scenario == 'installer-restart':
                        self.assertEqual(789, outcome['application_pid'])

    def test_guard_requests_installer_restart_and_keeps_fallback(self):
        self.assertIn('/CXVPNAUTORESTART=1', app_update._UPDATE_GUARD_PS1)
        self.assertIn('Find-UpdatedApplication $targetExe', app_update._UPDATE_GUARD_PS1)
        self.assertIn("Start-Process -FilePath $targetExe", app_update._UPDATE_GUARD_PS1)


if __name__ == '__main__':
    unittest.main()
