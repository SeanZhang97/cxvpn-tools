# -*- coding: utf-8 -*-
"""统一本地发行构建；不会创建 tag、提交代码或访问发布 API。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

from build_nuitka_cache import digest_file, emit


def payload_files(payload):
    payload = Path(payload)
    if not (payload / 'CXVPNTools.exe').is_file() or not (payload / '_internal').is_dir():
        raise RuntimeError('程序核心产物不完整')
    files = []
    for path in sorted(payload.rglob('*')):
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise RuntimeError(f'分发目录不允许链接: {path}')
        relative = path.relative_to(payload)
        name = path.name.lower()
        if (name in ('config.json', 'config.json.bak', 'auth.json') or
                (name.startswith('codex_') and name.endswith('snapshot.json')) or
                name.endswith(('.log', '.jsonl', '.db')) or '.sqlite' in name or
                any(part.lower() in ('webview_data', 'captcha_cache',
                                     'codex-session-provider-backups', 'recovery-backups') or
                    part.lower().startswith('browser_data') for part in relative.parts)):
            raise RuntimeError(f'分发目录包含用户数据: {relative}')
        if not path.is_file():
            continue
        files.append(path)
    return files


def create_core_zip(payload, target):
    payload, target = Path(payload), Path(target)
    files = payload_files(payload)
    with tempfile.TemporaryDirectory(prefix='release-', dir=target.parent) as temporary:
        archive = Path(temporary) / target.name
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as stream:
            for path in files:
                stream.write(path, (Path(payload.name) / path.relative_to(payload)).as_posix())
        with zipfile.ZipFile(archive) as stream:
            bad = stream.testzip()
            if bad:
                raise RuntimeError(f'ZIP 完整性检查失败: {bad}')
        os.replace(archive, target)
    return target


def write_manifest(root, version, artifacts, protected, offline):
    root = Path(root)
    records = []
    for path in artifacts:
        path = Path(path)
        digest = digest_file(path)
        path.with_name(path.name + '.sha256').write_text(
            f'{digest}  {path.name}\n', encoding='utf-8')
        records.append({'name': path.name, 'size': path.stat().st_size, 'sha256': digest})
    revision = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root,
                              check=True, capture_output=True, text=True,
                              encoding='utf-8', errors='strict', timeout=10).stdout.strip()
    status = subprocess.run(['git', 'status', '--porcelain=v1', '--untracked-files=normal'],
                            cwd=root, check=True, capture_output=True, text=True,
                            encoding='utf-8', errors='strict', timeout=10).stdout
    manifest = {'schema': 1, 'version': version, 'revision': revision,
                'source_dirty': bool(status.strip()),
                'protected': protected, 'offline_bundle': offline,
                'prerequisites_sha256': digest_file(root / 'installer' / 'prereqs.lock.json'),
                'artifacts': records}
    target = root / 'dist' / f'CXVPNTools-{version}-manifest.json'
    with tempfile.TemporaryDirectory(prefix='manifest-', dir=target.parent) as temporary:
        staged = Path(temporary) / target.name
        staged.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n',
                          encoding='utf-8')
        os.replace(staged, target)
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--standard', action='store_true', help='使用不保护业务代码的普通构建')
    parser.add_argument('--offline', action='store_true', help='额外生成完整离线安装器')
    args = parser.parse_args(argv)
    from core.desktop_runtime import ensure_desktop_runtime
    ensure_desktop_runtime(wait=True)
    from core.version import APP_VERSION
    root = Path(__file__).resolve().parent
    script = 'build.py' if args.standard else 'build_protected.py'
    emit(f'[release] 核心构建已提交: {script}; timeout=7200s')
    subprocess.run([sys.executable, str(root / script)], cwd=root, check=True, timeout=7200)
    from build_lifecycle import (capture_user_config_snapshot, complete_build,
                                 stop_running_apps)
    # 核心入口已验证并启动新版；发行阶段保持文件静止，结束后恢复该新版。
    # 不重复执行迁移或清理用户数据；发行阶段失败不代表已验证核心不能启动。
    stop_running_apps()
    snapshot = capture_user_config_snapshot()
    try:
        build_distribution(root, APP_VERSION, args)
    finally:
        complete_build(snapshot, str(root / 'dist' / 'CXVPNTools'))


def build_distribution(root, version, args):
    powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
    command = [str(powershell), '-NoProfile', '-NonInteractive', '-ExecutionPolicy',
               'Bypass', '-File', str(root / 'installer' / 'build_installer.ps1')]
    artifacts = []
    for offline in ([False, True] if args.offline else [False]):
        emit(f'[release] 安装器构建已提交: offline={offline}; timeout=1200s')
        subprocess.run(command + (['-Offline'] if offline else []), cwd=root,
                       check=True, timeout=1200)
        suffix = '-setup-offline.exe' if offline else '-setup.exe'
        artifacts.append(root / 'dist' / f'CXVPNTools-{version}{suffix}')
    emit('[release] 开始生成并校验程序核心 ZIP')
    artifacts.append(create_core_zip(root / 'dist' / 'CXVPNTools',
                                    root / 'dist' / f'CXVPNTools-{version}-windows-x64.zip'))
    manifest = write_manifest(root, version, artifacts, not args.standard, args.offline)
    emit(f'[release] 构建完成（未发布）: {manifest}')


if __name__ == '__main__':
    main()
