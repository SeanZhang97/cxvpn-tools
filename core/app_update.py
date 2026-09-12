# -*- coding: utf-8 -*-
"""应用更新：检查新版本、带进度下载升级包并驱动静默升级。"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser

try:  # Windows 专用；离线测试在缺库时仅跳过注册表相关断言。
    import winreg
except ImportError:  # pragma: no cover - 非 Windows 环境兜底
    winreg = None

from core import app_paths
from core.version import APP_NAME, APP_VERSION, GITHUB_REPOSITORY, is_newer


API_URL = f'https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest'
USER_AGENT = f'{APP_NAME}/{APP_VERSION}'
# 与 installer/CXVPNTools.iss 的 AppId 保持一致，用于定位升级后的安装目录。
UNINSTALL_REGISTRY_PATH = (
    r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'
    r'\{8D3D2C8E-6F03-4C2A-9B11-5B4F9E7A2B5E}_is1')
DOWNLOAD_CHUNK = 1024 * 1024
DOWNLOAD_READ_TIMEOUT = 30
PROGRESS_REPORT_INTERVAL = 0.25
INSTALL_MONITOR_TIMEOUT = 300
GUARD_TIMEOUT_SECONDS = 900


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
        return response.read()


def _asset_sha256(asset):
    """读取官方资产摘要（形如 sha256:<hex>）；缺失时返回空串跳过校验。"""
    digest = str((asset or {}).get('digest') or '').strip().lower()
    if digest.startswith('sha256:'):
        return digest.split(':', 1)[1]
    return ''


def check_latest():
    """读取最新稳定版本；失败时返回可展示的错误而不抛到 UI。"""
    try:
        payload = json.loads(_request(API_URL).decode('utf-8'))
        version = str(payload.get('tag_name') or '').lstrip('vV')
        assets = payload.get('assets') or []
        installer = next((item for item in assets
                          if str(item.get('name', '')).lower().endswith('.exe')), None)
        result = {
            'ok': True,
            'current_version': APP_VERSION,
            'latest_version': version,
            'newer': is_newer(version),
            'release_url': payload.get('html_url') or '',
            'installer_url': (installer or {}).get('browser_download_url') or '',
            'installer_name': (installer or {}).get('name') or '',
            'installer_sha256': _asset_sha256(installer),
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
    if not url.startswith(('https://github.com/', 'https://api.github.com/')):
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
    """下载升级包到用户数据目录；支持进度上报、取消与完整性校验。

    下载写入临时文件，全部完成后校验 SHA-256 并原子落盘，避免半截
    文件被误当作可执行安装包。
    """
    log = log or (lambda message: None)
    if not url.startswith('https://github.com/'):
        return {'ok': False, 'msg': '升级包地址不受信任'}
    target_root = updates_root()
    os.makedirs(target_root, exist_ok=True)
    filename = os.path.basename(url.split('?', 1)[0]) or 'update.exe'
    target = os.path.join(target_root, filename)
    temporary = tempfile.mktemp(prefix='update.', suffix='.tmp', dir=target_root)
    digest = hashlib.sha256()
    downloaded = 0
    total = 0
    started = time.monotonic()
    last_report = started
    last_reported_bytes = 0
    try:
        request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
        with urllib.request.urlopen(
                request, timeout=DOWNLOAD_READ_TIMEOUT) as source, \
                open(temporary, 'wb') as out:
            header_total = str(source.headers.get('Content-Length') or '')
            total = int(header_total) if header_total.isdigit() else 0
            if state is not None:
                state.update(phase='downloading', total_bytes=total, msg='')
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise _DownloadCancelled()
                chunk = source.read(DOWNLOAD_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                downloaded += len(chunk)
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
        if expected_sha256 and digest.hexdigest().lower() != expected_sha256.lower():
            raise ValueError('升级包完整性校验失败')
        os.replace(temporary, target)
        if state is not None:
            state.update(phase='downloaded', progress=100,
                         downloaded_bytes=downloaded, total_bytes=total,
                         speed_bps=0, path=target, msg='')
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
        if not name.lower().endswith('.exe'):
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
  [string]$RegistryPath,
  [string]$FallbackDir = '',
  [int]$TimeoutSeconds = 900
)
# 升级守护：静默安装完成后自动重启新版应用。
$ErrorActionPreference = 'SilentlyContinue'
if (-not $RegistryPath) {
  $RegistryPath = 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{8D3D2C8E-6F03-4C2A-9B11-5B4F9E7A2B5E}_is1'
}
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
while ((Get-Date) -lt $deadline) {
  Start-Sleep -Seconds 3
  $record = Get-ItemProperty -LiteralPath $RegistryPath -ErrorAction SilentlyContinue
  $version = if ($record) { [string]$record.DisplayVersion } else { '' }
  if ($version -ne $TargetVersion) { continue }
  $installDir = if ($record) { [string]$record.InstallLocation } else { '' }
  if (-not $installDir -and $FallbackDir) { $installDir = $FallbackDir }
  if (-not $installDir) { continue }
  $targetExe = Join-Path $installDir 'CXVPNTools.exe'
  if (-not (Test-Path -LiteralPath $targetExe)) { continue }
  Start-Sleep -Seconds 2
  Start-Process -FilePath $targetExe -WorkingDirectory $installDir
  exit 0
}
exit 1
'''


def _write_update_guard_script():
    target_root = updates_root()
    os.makedirs(target_root, exist_ok=True)
    script = os.path.join(target_root, 'apply-update.ps1')
    # PowerShell 5.1 依赖 BOM 识别 UTF-8 脚本，否则中文注释会按代码页误解。
    with open(script, 'w', encoding='utf-8-sig', newline='\r\n') as stream:
        stream.write(_UPDATE_GUARD_PS1)
    return script


def _powershell_exe():
    return os.path.join(
        os.environ.get('SystemRoot', r'C:\Windows'), 'System32',
        'WindowsPowerShell', 'v1.0', 'powershell.exe')


def launch_installer(installer_path, version, fallback_dir='', log=None):
    """启动静默升级安装器，并安排升级完成后的自动重启守护。

    安装器负责强制退出正在运行的旧版本并部署新文件；守护脚本轮询
    注册表版本号，出现目标版本后拉起新版应用。
    """
    log = log or (lambda message: None)
    installer_path = str(installer_path or '')
    if not os.path.isfile(installer_path):
        return {'ok': False, 'msg': '升级包文件不存在，请重新下载'}
    powershell = _powershell_exe()
    if not os.path.isfile(powershell):
        return {'ok': False, 'msg': '系统组件不可用，无法自动升级'}
    script = _write_update_guard_script()
    creationflags = (getattr(subprocess, 'DETACHED_PROCESS', 0) |
                     getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
    log('[app-update] 升级安装任务已提交: installer=%s version=%s guard_timeout=%ss' % (
        os.path.basename(installer_path), version, GUARD_TIMEOUT_SECONDS))
    guard = subprocess.Popen(
        [powershell, '-NoProfile', '-NonInteractive',
         '-ExecutionPolicy', 'Bypass', '-File', script,
         '-TargetVersion', str(version or ''),
         '-FallbackDir', str(fallback_dir or '')],
        creationflags=creationflags, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    installer = subprocess.Popen(
        [installer_path, '/VERYSILENT', '/NORESTART'],
        creationflags=creationflags, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log('[app-update] 升级安装器与守护进程已启动: installer_pid=%s guard_pid=%s' % (
        installer.pid, guard.pid))
    return {'ok': True, 'msg': '升级已开始，应用将自动退出并在完成后重启'}
