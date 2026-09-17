# -*- coding: utf-8 -*-
"""应用更新：检查新版本、带进度下载升级包并驱动静默升级。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
import uuid
import webbrowser

try:  # Windows 专用；离线测试在缺库时仅跳过注册表相关断言。
    import winreg
except ImportError:  # pragma: no cover - 非 Windows 环境兜底
    winreg = None

from core import app_paths
from core.version import (APP_NAME, APP_VERSION, GITHUB_REPOSITORY,
                          is_newer, normalized_version)


API_URL = f'https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest'
USER_AGENT = f'{APP_NAME}/{APP_VERSION}'
# 与 installer/CXVPNTools.iss 的 AppId 保持一致，用于定位升级后的安装目录。
UNINSTALL_REGISTRY_PATH = (
    r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'
    r'\{8D3D2C8E-6F03-4C2A-9B11-5B4F9E7A2B5E}_is1')
DOWNLOAD_CHUNK = 64 * 1024
DOWNLOAD_READ_TIMEOUT = 30
DOWNLOAD_TOTAL_TIMEOUT = 900
MAX_INSTALLER_BYTES = 512 * 1024 * 1024
PROGRESS_REPORT_INTERVAL = 0.25
INSTALL_MONITOR_TIMEOUT = 4260
GUARD_TIMEOUT_SECONDS = 4200


class _DownloadCancelled(Exception):
    """用户请求取消下载时在下载循环内抛出。"""


class UpdateDownloadState:
    """升级下载任务的线程安全状态容器，快照直接进入 UI 状态流。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._fields = self._initial_fields()

    @staticmethod
    def _initial_fields():
        return {
            'phase': 'idle',
            'version': '',
            'progress': 0,
            'downloaded_bytes': 0,
            'total_bytes': 0,
            'speed_bps': 0,
            'path': '',
            'sha256': '',
            'msg': '',
            'started_at': 0.0,
            'updated_at': 0.0,
        }

    def snapshot(self):
        with self._lock:
            return dict(self._fields)

    def update(self, **fields):
        with self._lock:
            self._fields.update(fields)
            self._fields['updated_at'] = time.time()

    def begin(self, version=''):
        with self._lock:
            self._fields = self._initial_fields()
            self._fields['phase'] = 'downloading'
            self._fields['version'] = str(version or '')
            self._fields['started_at'] = time.time()
            self._fields['updated_at'] = self._fields['started_at']


def _request(url, timeout=12):
    request = urllib.request.Request(url, headers={
        'Accept': 'application/vnd.github+json',
        'User-Agent': USER_AGENT,
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(1024 * 1024)


def _asset_sha256(asset):
    """读取完整 SHA-256；缺失时必须读取同版本校验附件，不能跳过校验。"""
    digest = str((asset or {}).get('digest') or '').strip().lower()
    if re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
        return digest.split(':', 1)[1]
    return ''


def installer_identity(url):
    """严格限制本仓库、版本标签与规范安装包名，不接受其它 EXE。"""
    parsed = urllib.parse.urlsplit(str(url or ''))
    if (parsed.scheme != 'https' or parsed.netloc != 'github.com'
            or parsed.query or parsed.fragment):
        raise ValueError('升级包地址不受信任')
    prefix = '/' + GITHUB_REPOSITORY + '/releases/download/'
    if not parsed.path.startswith(prefix):
        raise ValueError('升级包不属于本项目')
    parts = parsed.path[len(prefix):].split('/')
    if len(parts) != 2 or not re.fullmatch(r'[vV]?\d+\.\d+\.\d+', parts[0]):
        raise ValueError('升级包版本地址无效')
    version = normalized_version(parts[0])
    if parts[1] != f'{APP_NAME}-{version}-setup.exe':
        raise ValueError('升级包名称与版本不一致')
    return version, parts[1]


def _installer_digest(installer, assets):
    digest = _asset_sha256(installer)
    if digest:
        return digest
    name = installer['name']
    checksums = [item for item in assets if item.get('name') == name + '.sha256']
    expected_url = installer['browser_download_url'] + '.sha256'
    if len(checksums) != 1 or checksums[0].get('browser_download_url') != expected_url:
        return ''
    content = _request(expected_url).decode('utf-8-sig').strip()
    match = re.fullmatch(r'([0-9a-fA-F]{64})\s+\*?' + re.escape(name), content)
    return match[1].lower() if match else ''


def check_latest():
    """读取最新稳定版本；失败时返回可展示的错误而不抛到 UI。"""
    try:
        payload = json.loads(_request(API_URL).decode('utf-8'))
        if not isinstance(payload, dict):
            raise ValueError('版本响应无效')
        tag = str(payload.get('tag_name') or '')
        if payload.get('draft') or payload.get('prerelease') or not re.fullmatch(r'[vV]?\d+\.\d+\.\d+', tag):
            raise ValueError('不是有效的程序稳定版本')
        version = normalized_version(tag)
        assets = [item for item in (payload.get('assets') or []) if isinstance(item, dict)]
        candidates = [item for item in assets if item.get('name') == f'{APP_NAME}-{version}-setup.exe']
        installer = candidates[0] if len(candidates) == 1 else None
        digest = ''
        if installer:
            url = installer.get('browser_download_url', '')
            expected_url = f'https://github.com/{GITHUB_REPOSITORY}/releases/download/{tag}/{installer["name"]}'
            if url != expected_url:
                installer = None
            else:
                installer_identity(url)
                digest = _installer_digest(installer, assets)
        # 不提供未校验的自动执行入口；仍可告知用户有新版本。
        if not digest:
            installer = None
        result = {
            'ok': True,
            'current_version': APP_VERSION,
            'latest_version': version,
            'newer': is_newer(version),
            'release_url': f'https://github.com/{GITHUB_REPOSITORY}/releases/tag/{tag}',
            'installer_url': (installer or {}).get('browser_download_url') or '',
            'installer_name': (installer or {}).get('name') or '',
            'installer_sha256': digest,
            'notes': str(payload.get('body') or ''),
            'published_at': payload.get('published_at') or '',
        }
        return result
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {
                'ok': True,
                'current_version': APP_VERSION,
                'latest_version': '',
                'newer': False,
                'release_url': f'https://github.com/{GITHUB_REPOSITORY}/releases',
                'msg': '暂无可用更新：官方还没有发布过版本',
            }
        return {'ok': False, 'current_version': APP_VERSION,
                'msg': f'更新服务返回 HTTP {exc.code}'}
    except (OSError, ValueError, UnicodeError, urllib.error.URLError) as exc:
        return {'ok': False, 'current_version': APP_VERSION,
                'msg': f'检查更新失败：{type(exc).__name__}'}


def open_release(url):
    url = str(url or '').strip()
    if not url.startswith(f'https://github.com/{GITHUB_REPOSITORY}/releases'):
        return {'ok': False, 'msg': '更新地址不受信任'}
    try:
        webbrowser.open(url)
        return {'ok': True, 'msg': '已打开新版本下载页面'}
    except OSError as exc:
        return {'ok': False, 'msg': f'无法打开更新页面：{exc}'}


def updates_root():
    return os.path.join(app_paths.user_data_root(), 'updates')


def _remove(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def download_installer(url, expected_sha256='', state=None, cancel_event=None,
                       log=None):
    try:
        return _download_installer(url, expected_sha256, state, cancel_event, log)
    except (OSError, ValueError, TypeError) as exc:
        if log:
            log('[app-update] 下载准备失败: type=%s' % type(exc).__name__)
        return {'ok': False, 'msg': '下载升级包失败：' + type(exc).__name__}


def _download_installer(url, expected_sha256='', state=None, cancel_event=None,
                        log=None):
    """下载升级包到用户数据目录；支持进度上报、取消与完整性校验。

    下载写入临时文件，全部完成后校验 SHA-256 并原子落盘，避免半截
    文件被误当作可执行安装包。
    """
    log = log or (lambda message: None)
    try:
        version, filename = installer_identity(url)
    except ValueError as exc:
        return {'ok': False, 'msg': str(exc)}
    if cancel_event is not None and cancel_event.is_set():
        return {'ok': False, 'cancelled': True, 'msg': '已取消下载'}
    expected_sha256 = str(expected_sha256 or '').lower()
    if not re.fullmatch(r'[0-9a-f]{64}', expected_sha256):
        return {'ok': False, 'msg': '缺少有效的升级包校验值，请重新检查更新'}
    target_root = updates_root()
    os.makedirs(target_root, exist_ok=True)
    target = os.path.join(target_root, filename)
    if os.path.isfile(target):
        with open(target, 'rb') as existing:
            cached_digest = hashlib.file_digest(existing, 'sha256').hexdigest()
        if cached_digest == expected_sha256:
            size = os.path.getsize(target)
            if state is not None:
                state.update(phase='downloaded', progress=100, path=target,
                             sha256=expected_sha256, downloaded_bytes=size, total_bytes=size, speed_bps=0)
            log('[app-update] 已复用通过校验的本地升级包')
            return {'ok': True, 'path': target, 'sha256': expected_sha256, 'bytes': size}
    descriptor, temporary = tempfile.mkstemp(prefix='update.', suffix='.tmp', dir=target_root)
    os.close(descriptor)
    digest = hashlib.sha256()
    downloaded = 0
    total = 0
    started = time.monotonic()
    last_report = started
    last_reported_bytes = 0
    try:
        log('[app-update] 开始下载: version=%s read_timeout=%ss total_timeout=%ss' % (
            version, DOWNLOAD_READ_TIMEOUT, DOWNLOAD_TOTAL_TIMEOUT))
        request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
        with urllib.request.urlopen(
                request, timeout=DOWNLOAD_READ_TIMEOUT) as source, \
                open(temporary, 'wb') as out:
            header_total = str(source.headers.get('Content-Length') or '')
            total = int(header_total) if header_total.isdigit() else 0
            if total > MAX_INSTALLER_BYTES:
                raise ValueError('升级包超过大小限制')
            if state is not None:
                state.update(phase='downloading', total_bytes=total, msg='')
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise _DownloadCancelled()
                if time.monotonic() - started > DOWNLOAD_TOTAL_TIMEOUT:
                    raise TimeoutError('升级包下载超过总时限')
                chunk = source.read(DOWNLOAD_CHUNK)
                if time.monotonic() - started > DOWNLOAD_TOTAL_TIMEOUT:
                    raise TimeoutError('升级包下载超过总时限')
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                downloaded += len(chunk)
                if downloaded > MAX_INSTALLER_BYTES or (total and downloaded > total):
                    raise ValueError('升级包大小超出预期')
                now = time.monotonic()
                if state is not None and now - last_report >= PROGRESS_REPORT_INTERVAL:
                    elapsed = max(now - last_report, 1e-6)
                    progress = int(downloaded * 100 / total) if total else 0
                    state.update(
                        progress=progress,
                        downloaded_bytes=downloaded,
                        total_bytes=total,
                        speed_bps=int((downloaded - last_reported_bytes) / elapsed))
                    last_report = now
                    last_reported_bytes = downloaded
            out.flush()
            os.fsync(out.fileno())
        if total and downloaded != total:
            raise ValueError('升级包下载不完整')
        if cancel_event is not None and cancel_event.is_set():
            raise _DownloadCancelled()
        if digest.hexdigest().lower() != expected_sha256:
            raise ValueError('升级包完整性校验失败')
        os.replace(temporary, target)
        if state is not None:
            state.update(phase='downloaded', progress=100,
                         downloaded_bytes=downloaded, total_bytes=total,
                         speed_bps=0, path=target, sha256=expected_sha256, msg='')
        log('[app-update] 升级包下载完成: bytes=%d sha256=%s elapsed=%.1fs' % (
            downloaded, digest.hexdigest()[:12], time.monotonic() - started))
        return {'ok': True, 'path': target, 'sha256': digest.hexdigest(),
                'bytes': downloaded}
    except _DownloadCancelled:
        _remove(temporary)
        return {'ok': False, 'cancelled': True, 'msg': '已取消下载'}
    except Exception as exc:
        _remove(temporary)
        log('[app-update] 升级包下载失败: type=%s elapsed=%.1fs' % (
            type(exc).__name__, time.monotonic() - started))
        return {'ok': False, 'msg': f'下载升级包失败：{type(exc).__name__}'}


def prune_old_installers(keep_path, log=None):
    """清理历史升级包，仅保留本次下载的安装器与守护脚本。"""
    root = updates_root()
    if not os.path.isdir(root):
        return
    for name in sorted(os.listdir(root)):
        if not re.fullmatch(re.escape(APP_NAME) + r'-\d+\.\d+\.\d+-setup\.exe', name):
            continue
        candidate = os.path.join(root, name)
        if candidate == keep_path:
            continue
        _remove(candidate)


def installed_display_version():
    """读取注册表中已安装版本号；未安装或读取失败返回空串。"""
    if winreg is None:
        return ''
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            UNINSTALL_REGISTRY_PATH) as key:
            value, _ = winreg.QueryValueEx(key, 'DisplayVersion')
            return str(value or '')
    except OSError:
        return ''


_UPDATE_GUARD_PS1 = '''param(
  [Parameter(Mandatory = $true)][string]$TargetVersion,
  [Parameter(Mandatory = $true)][string]$Installer,
  [Parameter(Mandatory = $true)][string]$ExpectedSHA256,
  [Parameter(Mandatory = $true)][string]$ResultPath,
  [string]$RegistryPath,
  [string]$FallbackDir = '',
  [int]$TimeoutSeconds = 4200
)
# Only this unelevated guard launches the application after the elevated installer exits.
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
function Write-Result([string]$Phase, [string]$Message, [int]$Code = 0) {
  $fields = @{phase=$Phase; version=$TargetVersion; message=$Message; exit_code=$Code}
  if ($null -ne $process) { $fields.installer_pid = $process.Id; $fields.installer_started = $installerStarted }
  if ($null -ne $application) { $fields.application_pid = $application.Id }
  $json = $fields | ConvertTo-Json -Compress
  $temporary = $ResultPath + '.tmp'
  [IO.File]::WriteAllText($temporary, $json, [Text.UTF8Encoding]::new($false))
  Move-Item -LiteralPath $temporary -Destination $ResultPath -Force
}
function Find-UpdatedApplication([string]$ExecutablePath) {
  $expected = [IO.Path]::GetFullPath($ExecutablePath)
  foreach ($candidate in @(Get-Process -Name 'CXVPNTools' -ErrorAction SilentlyContinue)) {
    try {
      if ([String]::Equals([IO.Path]::GetFullPath($candidate.Path), $expected,
          [StringComparison]::OrdinalIgnoreCase)) { return $candidate }
    } catch { }
  }
  return $null
}
if (-not $RegistryPath) {
  $RegistryPath = 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{8D3D2C8E-6F03-4C2A-9B11-5B4F9E7A2B5E}_is1'
}
$process = $null
$application = $null
$installerStarted = 0
try {
  Write-Result 'starting' 'Preparing verified installer'
  if ((Get-FileHash -LiteralPath $Installer -Algorithm SHA256).Hash -ne $ExpectedSHA256) {
    throw 'Installer changed after download'
  }
  # The installer starts the updated app as the original desktop user. The
  # unelevated guard remains a fallback and verifies the resulting process.
  $arguments = '/VERYSILENT /NORESTART /RESTARTEXITCODE=3010 /CXVPNAUTORESTART=1'
  $process = Start-Process -FilePath $Installer -ArgumentList $arguments -Verb RunAs -WindowStyle Hidden -PassThru
  $installerStarted = $process.StartTime.ToUniversalTime().ToFileTimeUtc()
  Write-Result 'installing' 'Installer started; preparing dependencies before stopping the old application'
  if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
    # Never kill a live installer halfway through file replacement.
    Write-Result 'timed_out' 'Installer is still running; inspect it before retrying' 1460
    exit 1
  }
  $code = $process.ExitCode
  if ($code -eq 8 -or $code -eq 3010 -or $code -eq 1641) {
    Write-Result 'restart_required' 'Restart Windows to finish installation' $code
    exit 0
  }
  if ($code -ne 0) { Write-Result 'failed' 'Installer failed or was cancelled' $code; exit 1 }
  $record = Get-ItemProperty -LiteralPath $RegistryPath -ErrorAction SilentlyContinue
  $version = if ($record) { [string]$record.DisplayVersion } else { '' }
  if ($version -ne $TargetVersion) { throw 'Installed version verification failed' }
  $installDir = if ($record) { [string]$record.InstallLocation } else { '' }
  if (-not $installDir -and $FallbackDir) { $installDir = $FallbackDir }
  if (-not $installDir) { throw 'Installation directory unavailable' }
  $targetExe = Join-Path $installDir 'CXVPNTools.exe'
  if (-not (Test-Path -LiteralPath $targetExe)) { throw 'Installed executable missing' }
  if ((Get-Item -LiteralPath $targetExe).VersionInfo.ProductVersion -ne $TargetVersion) {
    throw 'Executable version verification failed'
  }
  $application = Find-UpdatedApplication $targetExe
  if ($null -eq $application) {
    $application = Start-Process -FilePath $targetExe -WorkingDirectory $installDir -PassThru
  }
  Start-Sleep -Seconds 5
  $verifiedApplication = Find-UpdatedApplication $targetExe
  if ($null -eq $verifiedApplication) { throw 'Updated application exited during startup' }
  if ($verifiedApplication.Id -ne $application.Id) {
    $application.Dispose()
    $application = $verifiedApplication
  } else {
    $verifiedApplication.Dispose()
  }
  Write-Result 'completed' 'Verified application restarted'
  exit 0
} catch {
  Write-Result 'failed' ('Update failed: ' + $_.Exception.GetType().Name) 9001
  exit 1
} finally {
  if ($null -ne $application) { $application.Dispose() }
  if ($null -ne $process) { $process.Dispose() }
}
'''


def _write_update_guard_script(attempt):
    target_root = updates_root()
    os.makedirs(target_root, exist_ok=True)
    script = os.path.join(target_root, f'apply-update-{attempt}.ps1')
    # PowerShell 5.1 依赖 BOM 识别 UTF-8 脚本，否则中文注释会按代码页误解。
    with open(script, 'w', encoding='utf-8-sig', newline='\r\n') as stream:
        stream.write(_UPDATE_GUARD_PS1)
    return script


def _powershell_exe():
    return os.path.join(
        os.environ.get('SystemRoot', r'C:\Windows'), 'System32',
        'WindowsPowerShell', 'v1.0', 'powershell.exe')


def launch_installer(installer_path, version, fallback_dir='', log=None,
                     expected_sha256=''):
    try:
        return _launch_installer(installer_path, version, fallback_dir, log, expected_sha256)
    except (OSError, ValueError, TypeError) as exc:
        if log:
            log('[app-update] 升级准备失败: type=%s' % type(exc).__name__)
        return {'ok': False, 'msg': '升级准备失败，旧版保持运行：' + type(exc).__name__}


def _launch_installer(installer_path, version, fallback_dir='', log=None,
                      expected_sha256=''):
    """启动静默升级安装器，并安排升级完成后的自动重启守护。

    安装器先准备依赖再退出旧版；守护脚本等待安装进程真正退出，
    同时核对退出码、注册表和 EXE 版本后才拉起新版。
    """
    log = log or (lambda message: None)
    installer_path = str(installer_path or '')
    if not os.path.isfile(installer_path):
        return {'ok': False, 'msg': '升级包文件不存在，请重新下载'}
    version = str(version or '')
    if (not re.fullmatch(r'\d+\.\d+\.\d+', version) or
            os.path.basename(installer_path) != f'{APP_NAME}-{version}-setup.exe' or
            not re.fullmatch(r'[0-9a-fA-F]{64}', str(expected_sha256 or ''))):
        return {'ok': False, 'msg': '升级包身份或校验值无效，请重新检查更新'}
    try:
        with open(installer_path, 'rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != expected_sha256.lower():
                return {'ok': False, 'msg': '升级包已变化，请重新下载'}
    except OSError:
        return {'ok': False, 'msg': '升级包无法读取，请重新下载'}
    powershell = _powershell_exe()
    if not os.path.isfile(powershell):
        return {'ok': False, 'msg': '系统组件不可用，无法自动升级'}
    attempt = uuid.uuid4().hex
    script = _write_update_guard_script(attempt)
    result_path = os.path.join(updates_root(), f'update-result-{attempt}.json')
    creationflags = (getattr(subprocess, 'DETACHED_PROCESS', 0) |
                     getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
    log('[app-update] 升级安装任务已提交: installer=%s version=%s guard_timeout=%ss' % (
        os.path.basename(installer_path), version, GUARD_TIMEOUT_SECONDS))
    try:
        guard = subprocess.Popen(
            [powershell, '-NoProfile', '-NonInteractive',
             '-ExecutionPolicy', 'Bypass', '-File', script,
             '-TargetVersion', version, '-Installer', installer_path,
             '-ExpectedSHA256', expected_sha256,
             '-ResultPath', result_path,
             '-TimeoutSeconds', str(GUARD_TIMEOUT_SECONDS),
             '-FallbackDir', str(fallback_dir or '')],
            creationflags=creationflags | getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        log('[app-update] 升级守护启动失败: type=%s' % type(exc).__name__)
        return {'ok': False, 'msg': '升级进程未能启动，旧版保持运行'}
    log('[app-update] 升级守护已启动: guard_pid=%s' % guard.pid)
    return {'ok': True, 'result_path': result_path, '_guard': guard,
            'msg': '升级已提交；组件准备完成后应用将退出，成功安装后自动重启'}


def read_install_result(path):
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding='utf-8-sig') as stream:
            result = json.loads(stream.read(16384))
        return result if isinstance(result, dict) else None
    except (OSError, ValueError):
        return None


def installer_process_running(result):
    """确认超时安装进程身份；不可确认时返回 None，不允许并发重试。"""
    if os.name != 'nt' or not result:
        return None
    pid, started = result.get('installer_pid'), result.get('installer_started')
    if not isinstance(pid, int) or pid <= 0 or not isinstance(started, int) or started <= 0:
        return None
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False if ctypes.get_last_error() == 87 else None
    try:
        creation, ended, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
        code = wintypes.DWORD()
        if not kernel.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(ended),
                                      ctypes.byref(kernel_time), ctypes.byref(user_time)):
            return None
        identity = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        if identity != started:
            return False
        return (code.value == 259 if kernel.GetExitCodeProcess(handle, ctypes.byref(code)) else None)
    finally:
        kernel.CloseHandle(handle)
