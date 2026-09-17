# -*- coding: utf-8 -*-
"""在 Windows 当前用户会话中安全重启 Codex 桌面应用。"""
from __future__ import annotations

import base64
import json
import subprocess
import time

from core import codex_session_provider


RESTART_TIMEOUT_SECONDS = 35
STOP_TIMEOUT_SECONDS = 18
START_TIMEOUT_SECONDS = 17

_STOP_SCRIPT = r'''
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Text.UTF8Encoding]::new($false)
$ErrorActionPreference = 'Stop'

$package = Get-AppxPackage -Name 'OpenAI.Codex' |
  Sort-Object Version -Descending |
  Select-Object -First 1
if ($null -eq $package) {
  throw '未找到 OpenAI.Codex 应用包'
}
$installRoot = [IO.Path]::GetFullPath($package.InstallLocation).TrimEnd('\') + '\'

function Get-CodexDesktopProcesses {
  @(
    Get-CimInstance Win32_Process -Filter "Name = 'ChatGPT.exe'" |
      Where-Object {
        try {
          $_.ExecutablePath -and
            [IO.Path]::GetFullPath($_.ExecutablePath).StartsWith(
              $installRoot, [StringComparison]::OrdinalIgnoreCase)
        } catch {
          $false
        }
      }
  )
}

$targets = @(Get-CodexDesktopProcesses)
$wasRunning = $targets.Count -gt 0
$gracefulRequests = 0
foreach ($target in $targets) {
  try {
    $process = Get-Process -Id $target.ProcessId -ErrorAction Stop
    if ($process.MainWindowHandle -ne 0 -and $process.CloseMainWindow()) {
      $gracefulRequests++
    }
  } catch {
  }
}

$gracefulDeadline = [DateTime]::UtcNow.AddSeconds(4)
do {
  $remaining = @(Get-CodexDesktopProcesses)
  if ($remaining.Count -eq 0) { break }
  Start-Sleep -Milliseconds 200
} while ([DateTime]::UtcNow -lt $gracefulDeadline)

$remaining = @(Get-CodexDesktopProcesses)
$forcedProcesses = $remaining.Count
foreach ($target in $remaining) {
  Stop-Process -Id $target.ProcessId -Force -ErrorAction SilentlyContinue
}

$stopDeadline = [DateTime]::UtcNow.AddSeconds(8)
do {
  $remaining = @(Get-CodexDesktopProcesses)
  if ($remaining.Count -eq 0) { break }
  Start-Sleep -Milliseconds 200
} while ([DateTime]::UtcNow -lt $stopDeadline)
if ((Get-CodexDesktopProcesses).Count -gt 0) {
  throw 'Codex 进程未在超时时间内退出'
}

[pscustomobject]@{
  ok = $true
  was_running = $wasRunning
  graceful_requests = $gracefulRequests
  forced_processes = $forcedProcesses
} | ConvertTo-Json -Compress
'''

_START_SCRIPT = r'''
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Text.UTF8Encoding]::new($false)
$ErrorActionPreference = 'Stop'

$package = Get-AppxPackage -Name 'OpenAI.Codex' |
  Sort-Object Version -Descending |
  Select-Object -First 1
if ($null -eq $package) {
  throw '未找到 OpenAI.Codex 应用包'
}
$installRoot = [IO.Path]::GetFullPath($package.InstallLocation).TrimEnd('\') + '\'

function Get-CodexDesktopProcesses {
  @(
    Get-CimInstance Win32_Process -Filter "Name = 'ChatGPT.exe'" |
      Where-Object {
        try {
          $_.ExecutablePath -and
            [IO.Path]::GetFullPath($_.ExecutablePath).StartsWith(
              $installRoot, [StringComparison]::OrdinalIgnoreCase)
        } catch {
          $false
        }
      }
  )
}

$appUserModelId = "$($package.PackageFamilyName)!App"
Start-Process -FilePath "$env:SystemRoot\explorer.exe" `
  -ArgumentList "shell:AppsFolder\$appUserModelId" | Out-Null

$startDeadline = [DateTime]::UtcNow.AddSeconds(15)
$mainProcess = $null
do {
  $mainProcess = Get-CodexDesktopProcesses |
    Where-Object { $_.CommandLine -notmatch '(?i)(^|\s)--type=' } |
    Select-Object -First 1
  if ($null -ne $mainProcess) { break }
  Start-Sleep -Milliseconds 250
} while ([DateTime]::UtcNow -lt $startDeadline)
if ($null -eq $mainProcess) {
  throw 'Codex 未在超时时间内重新启动'
}

[pscustomobject]@{
  ok = $true
  pid = [int]$mainProcess.ProcessId
} | ConvertTo-Json -Compress
'''


def _log(logger, message):
    if logger:
        try:
            logger(message)
        except Exception:
            pass


def _encoded_script(script):
    return base64.b64encode(
        script.encode('utf-16-le')).decode('ascii')


def _run_script(runner, script, timeout, failure_message):
    completed = runner(
        ['powershell.exe', '-NoProfile', '-NonInteractive',
         '-EncodedCommand', _encoded_script(script)],
        timeout=timeout,
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='backslashreplace',
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )
    if completed.returncode != 0:
        raise RuntimeError(failure_message)
    lines = [line.strip().lstrip('\ufeff')
             for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise ValueError(f'{failure_message}：没有返回结果')
    result = json.loads(lines[-1])
    if not isinstance(result, dict) or result.get('ok') is not True:
        raise ValueError(f'{failure_message}：返回结果无效')
    return result


def restart_codex(*, logger=None, runner=None, environ=None, data_root=None,
                  provider_migrator=None):
    """停止 Codex、同步历史任务 Provider，再启动桌面应用。"""
    started = time.monotonic()
    _log(logger, '[codex-runtime] 重启请求已提交')
    _log(logger, '[codex-runtime] 请求已领取，开始执行：目标=OpenAI.Codex，超时=35秒')
    run = runner or subprocess.run
    try:
        stopped = _run_script(
            run, _STOP_SCRIPT, STOP_TIMEOUT_SECONDS,
            'Windows 未能关闭 Codex')
        was_running = bool(stopped.get('was_running'))
        forced = max(0, int(stopped.get('forced_processes') or 0))
        migrate = provider_migrator or codex_session_provider.migrate_current_provider
        try:
            migration = migrate(
                environ=environ, data_root=data_root, logger=logger)
            if not isinstance(migration, dict):
                raise TypeError('历史任务 Provider 同步返回结果无效')
        except Exception as exc:
            migration = {
                'ok': False,
                'warning': (
                    '历史任务 Provider 同步失败：'
                    f'{type(exc).__name__}'),
            }
            _log(
                logger,
                f'[codex-runtime] 历史任务同步异常，继续启动 Codex：'
                f'类型={type(exc).__name__}')
        started_result = _run_script(
            run, _START_SCRIPT, START_TIMEOUT_SECONDS,
            'Windows 未能启动 Codex')
        if not migration.get('ok'):
            warning = (
                'Codex 已重新启动，但' +
                (migration.get('warning') or '历史任务 Provider 同步失败'))
            _log(
                logger,
                f'[codex-runtime] 重启部分完成：历史任务同步失败，'
                f'耗时={time.monotonic() - started:.2f}秒')
            return {
                'ok': False,
                'warning': warning,
                'app_started': True,
                'was_running': was_running,
                'forced_processes': forced,
                'provider_migration': migration,
            }
        updated_threads = max(0, int(migration.get('updated_threads') or 0))
        message = 'Codex 已重新启动' if was_running else 'Codex 已启动'
        if updated_threads:
            message += f'，已同步 {updated_threads} 个历史任务的 Provider'
        _log(
            logger,
            f'[codex-runtime] 重启成功：原状态='
            f'{"运行中" if was_running else "未运行"}，强制结束={forced}，'
            f'历史任务={updated_threads}，pid={started_result.get("pid")}，'
            f'耗时={time.monotonic() - started:.2f}秒')
        return {
            'ok': True,
            'msg': message,
            'was_running': was_running,
            'forced_processes': forced,
            'provider_migration': migration,
        }
    except subprocess.TimeoutExpired:
        warning = 'Codex 重启超时，请手动检查应用状态'
        error_type = 'TimeoutExpired'
    except (OSError, RuntimeError, ValueError, TypeError,
            json.JSONDecodeError) as exc:
        warning = str(exc)
        error_type = type(exc).__name__
    _log(
        logger,
        f'[codex-runtime] 重启失败：类型={error_type}，'
        f'耗时={time.monotonic() - started:.2f}秒')
    return {'ok': False, 'warning': warning}
