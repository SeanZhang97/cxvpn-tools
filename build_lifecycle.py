# -*- coding: utf-8 -*-
"""Windows 打包前后的进程收敛、用户配置核对与新版启动。"""
from __future__ import annotations

import base64
from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time

from core import app_paths


APP_PROCESS_NAMES = ('CXVPNTools', 'CX VPN TOOLS', 'CXVPN管理器')
STATE_BACKUP_LIMIT = 5


class BuildLifecycleError(RuntimeError):
    pass


def _powershell(script, timeout=20):
    encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
    completed = subprocess.run(
        ['powershell.exe', '-NoProfile', '-NonInteractive',
         '-EncodedCommand', encoded],
        check=False, capture_output=True, text=True,
        encoding='utf-8', errors='backslashreplace', timeout=timeout)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or '').strip()
        raise BuildLifecycleError(
            f'关闭 CXVPNTools 运行实例失败: {detail or completed.returncode}')
    return completed.stdout


def stop_running_apps():
    names = ','.join("'%s'" % name.replace("'", "''")
                     for name in APP_PROCESS_NAMES)
    script = f"""
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$ErrorActionPreference = 'Stop'
$names = @({names})
$targets = @(Get-Process -ErrorAction SilentlyContinue |
  Where-Object {{ $names -contains $_.ProcessName }})
$roots = @($targets | ForEach-Object {{
  try {{
    if ($_.Path) {{ Split-Path -LiteralPath $_.Path -Parent }}
  }} catch {{}}
}} | Sort-Object -Unique)
foreach ($target in $targets) {{
  Stop-Process -Id $target.Id -Force -ErrorAction Stop
}}
$deadline = [DateTime]::UtcNow.AddSeconds(15)
do {{
  $remaining = @(Get-Process -ErrorAction SilentlyContinue |
    Where-Object {{ $names -contains $_.ProcessName }})
  if ($remaining.Count -eq 0) {{ break }}
  Start-Sleep -Milliseconds 200
}} while ([DateTime]::UtcNow -lt $deadline)
if ($remaining.Count -ne 0) {{
  $ids = ($remaining | ForEach-Object {{ $_.Id }}) -join ','
  Write-Error "CXVPNTools 进程未在期限内退出: $ids"
  exit 3
}}
foreach ($root in $roots) {{
  [Console]::Out.WriteLine("CXVPN_RUNNING_ROOT=$root")
}}
exit 0
"""
    print('[build] 正在关闭 CXVPNTools 新旧运行实例，等待期限 15 秒')
    output = _powershell(script, timeout=20)
    print('[build] CXVPNTools 运行实例已全部退出')
    prefix = 'CXVPN_RUNNING_ROOT='
    return [
        os.path.abspath(line[len(prefix):].strip())
        for line in (output.splitlines() if isinstance(output, str) else [])
        if line.startswith(prefix) and line[len(prefix):].strip()]


def _file_digest(path):
    if not os.path.isfile(path):
        return None
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _database_config_digest(path):
    if not os.path.isfile(path):
        return None
    uri = Path(path).resolve().as_uri() + '?mode=ro'
    try:
        with closing(sqlite3.connect(
                uri, uri=True, timeout=5.0)) as conn:
            conn.execute('PRAGMA query_only=ON')
            row = conn.execute(
                'SELECT revision,payload FROM kv_config WHERE id=1').fetchone()
    except sqlite3.Error as exc:
        raise BuildLifecycleError(
            f'读取 LocalAppData 主配置失败: {type(exc).__name__}') from exc
    if row is None:
        return None
    return {
        'revision': int(row[0]),
        'payload_sha256': hashlib.sha256(
            str(row[1]).encode('utf-8')).hexdigest(),
    }


def capture_user_config_snapshot(data_root=None):
    root = os.path.abspath(data_root or app_paths.user_data_root())
    return {
        'root': root,
        'database': _database_config_digest(
            os.path.join(root, app_paths.STATE_DATABASE_NAME)),
        'config_json_sha256': _file_digest(os.path.join(root, 'config.json')),
    }


def create_user_state_backup(data_root=None):
    """用 SQLite backup API 保存构建前一致性副本，避免遗漏 WAL。"""
    root = os.path.abspath(data_root or app_paths.user_data_root())
    source_path = os.path.join(root, app_paths.STATE_DATABASE_NAME)
    if not os.path.isfile(source_path):
        return None
    backup_root = os.path.join(root, 'recovery-backups')
    os.makedirs(backup_root, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    target_path = os.path.join(backup_root, f'state-before-build-{stamp}.sqlite3')
    descriptor, temporary = tempfile.mkstemp(
        prefix='state-before-build-', suffix='.tmp', dir=backup_root)
    os.close(descriptor)
    try:
        source_uri = Path(source_path).resolve().as_uri() + '?mode=ro'
        with closing(sqlite3.connect(
                source_uri, uri=True, timeout=5.0)) as source, \
                closing(sqlite3.connect(temporary)) as target:
            source.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise BuildLifecycleError('构建前用户状态备份完整性检查失败')
        os.replace(temporary, target_path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    backups = sorted(
        (entry for entry in os.scandir(backup_root)
         if entry.is_file(follow_symlinks=False) and
         entry.name.startswith('state-before-build-') and
         entry.name.endswith('.sqlite3')),
        key=lambda entry: (entry.stat().st_mtime_ns, entry.name),
        reverse=True)
    for entry in backups[STATE_BACKUP_LIMIT:]:
        os.unlink(entry.path)
    print(f'[build] 已保存构建前用户状态备份: {target_path}')
    return target_path


def verify_user_config_unchanged(before):
    after = capture_user_config_snapshot(before['root'])
    for key in ('database', 'config_json_sha256'):
        if before[key] is not None and before[key] != after[key]:
            raise BuildLifecycleError(
                f'打包期间 LocalAppData 用户配置发生变化: {key}')
    revision = ((after.get('database') or {}).get('revision'))
    print('[build] LocalAppData 用户配置核对通过%s' % (
        f'，revision={revision}' if revision is not None else ''))
    return after


def prepare_build():
    running_roots = stop_running_apps()
    create_user_state_backup()
    snapshot = capture_user_config_snapshot()
    snapshot['running_roots'] = running_roots
    return snapshot


def start_packaged_app(dist_root, app_name='CXVPNTools'):
    executable = os.path.abspath(os.path.join(dist_root, app_name + '.exe'))
    if not os.path.isfile(executable):
        raise BuildLifecycleError(f'打包产物不存在: {executable}')
    creation_flags = (getattr(subprocess, 'DETACHED_PROCESS', 0) |
                      getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
    process = subprocess.Popen(
        [executable], cwd=os.path.dirname(executable),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, close_fds=True,
        creationflags=creation_flags)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        code = process.poll()
        if code is not None:
            raise BuildLifecycleError(
                f'新版启动后提前退出: path={executable}, exit={code}')
        time.sleep(0.2)
    print(f'[build] 新版已启动: path={executable}, pid={process.pid}')
    return process.pid


def complete_build(before, dist_root, app_name='CXVPNTools'):
    verify_user_config_unchanged(before)
    return start_packaged_app(dist_root, app_name)
