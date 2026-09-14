# -*- coding: utf-8 -*-
"""应用资源目录、当前用户数据目录与旧版数据迁移。"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile


APP_NAME = 'CXVPNTools'
LEGACY_APP_DATA_NAME = 'CXVPNManager'
USER_DATA_FILES = (
    'config.json',
    'run.log',
    'startup.log',
    'netlog.jsonl',
    'proxy_guard.json',
    'proxy_guard_snapshot.json',
    'codex_proxy_snapshot.json',
)
USER_DATA_DIRS = (
    'config-history',
    'webview_data',
    'browser_data',
    'captcha_cache',
    'routing',
    'routing_data',
    'rule-packs',
)
RULE_PACK_FILES = ('local-direct-v1.txt', 'cn-direct-v1.txt')
MIGRATION_BACKUP_DIR = 'migration-backups'
STATE_DATABASE_NAME = 'state.sqlite3'


def resource_root():
    """返回只读资源根目录；打包后指向 PyInstaller 的 _internal。"""
    if getattr(sys, 'frozen', False):
        return getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def install_root():
    """返回可执行文件所在目录；源码运行时返回仓库根目录。"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return resource_root()


def user_data_root(environ=None):
    """返回当前 Windows 用户的机器本地数据目录。"""
    env = os.environ if environ is None else environ
    local = str(env.get('LOCALAPPDATA') or '').strip()
    if not local:
        profile = str(env.get('USERPROFILE') or os.path.expanduser('~')).strip()
        local = os.path.join(profile, 'AppData', 'Local')
    return os.path.abspath(os.path.join(local, APP_NAME))


def _same_path(first, second):
    return os.path.normcase(os.path.abspath(first)) == os.path.normcase(
        os.path.abspath(second))


def _copy_file_if_missing(source, target, copied, warnings):
    if not os.path.isfile(source) or os.path.exists(target):
        return
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(source, target)
        copied.append(target)
    except OSError as exc:
        warnings.append(f'{source}: {type(exc).__name__}: {exc}')


def _file_digest(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _valid_config_file(path):
    try:
        with open(path, encoding='utf-8-sig') as stream:
            return isinstance(json.load(stream), dict)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False


def _preserve_config_copy(source, target_root, digest, copied, warnings):
    """把即将被舍弃的冲突配置保存在用户目录，返回备份路径。"""
    modified_ns = os.stat(source).st_mtime_ns
    backup = os.path.join(
        target_root, MIGRATION_BACKUP_DIR,
        f'config-{modified_ns}-{digest[:12]}.json')
    _copy_file_if_missing(source, backup, copied, warnings)
    return backup if os.path.isfile(backup) else ''


def _replace_file(source, target):
    directory = os.path.dirname(target)
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix='migration.', suffix='.tmp', dir=directory)
    os.close(descriptor)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _merge_config_file(source, target, target_root, copied, replaced,
                       conflicts, blocking, warnings, allow_replace=True):
    """合并主配置；不同内容按有效性和修改时间择新，并保留另一份。"""
    if not os.path.isfile(source) or os.path.islink(source):
        return
    if not os.path.exists(target):
        _copy_file_if_missing(source, target, copied, warnings)
        return
    try:
        source_digest = _file_digest(source)
        target_digest = _file_digest(target)
        if source_digest == target_digest:
            return

        source_valid = _valid_config_file(source)
        target_valid = _valid_config_file(target)
        source_newer = os.stat(source).st_mtime_ns > os.stat(target).st_mtime_ns
        use_source = (allow_replace and source_valid and
                      (not target_valid or source_newer))
        discarded = target if use_source else source
        discarded_digest = target_digest if use_source else source_digest
        backup = _preserve_config_copy(
            discarded, target_root, discarded_digest, copied, warnings)
        if not backup:
            blocking.append(
                f'配置冲突副本保存失败，已保留当前配置: {source}')
            conflicts.append({
                'source': source,
                'target': target,
                'kept': target,
                'backup': '',
                'reason': 'backup-failed',
            })
            return
        conflicts.append({
            'source': source,
            'target': target,
            'kept': source if use_source else target,
            'backup': backup,
            'reason': ('canonical-state-present' if not allow_replace else
                       'target-invalid' if use_source and not target_valid else
                       'source-newer' if use_source else
                       'source-invalid' if not source_valid else
                       'target-newer-or-equal'),
        })
        if use_source:
            _replace_file(source, target)
            replaced.append(target)
    except OSError as exc:
        warnings.append(f'{source}: {type(exc).__name__}: {exc}')


def _merge_directory(source, target, copied, warnings):
    if not os.path.isdir(source) or _same_path(source, target):
        return
    try:
        entries = list(os.scandir(source))
    except OSError as exc:
        warnings.append(f'{source}: {type(exc).__name__}: {exc}')
        return
    for entry in entries:
        if entry.is_symlink():
            continue
        child_target = os.path.join(target, entry.name)
        try:
            if entry.is_dir(follow_symlinks=False):
                _merge_directory(entry.path, child_target, copied, warnings)
            elif entry.is_file(follow_symlinks=False):
                _copy_file_if_missing(
                    entry.path, child_target, copied, warnings)
        except OSError as exc:
            warnings.append(
                f'{entry.path}: {type(exc).__name__}: {exc}')


def migrate_legacy_user_data(data_root=None, legacy_roots=None,
                             legacy_local_root=None,
                             bundled_rule_pack_root=None):
    """首次建立主库前，把旧数据合并到当前用户目录。

    主库已存在时不再读取安装目录或旧产品目录中的用户数据；资源规则包
    仍可补齐。首次迁移不删除旧数据，也不跟随符号链接。
    """
    target_root = os.path.abspath(data_root or user_data_root())
    copied = []
    replaced = []
    conflicts = []
    blocking = []
    warnings = []
    try:
        os.makedirs(target_root, exist_ok=True)
    except OSError as exc:
        return {
            'data_root': target_root,
            'copied': copied,
            'replaced': replaced,
            'conflicts': conflicts,
            'blocking': blocking,
            'warnings': [f'{target_root}: {type(exc).__name__}: {exc}'],
        }

    canonical_state_exists = os.path.isfile(
        os.path.join(target_root, STATE_DATABASE_NAME))
    if canonical_state_exists:
        roots = []
    elif legacy_roots is None:
        roots = [install_root(), resource_root()]
    else:
        roots = list(legacy_roots)
    seen = set()
    for root in roots:
        if not root:
            continue
        source_root = os.path.abspath(root)
        key = os.path.normcase(source_root)
        if key in seen or _same_path(source_root, target_root):
            continue
        seen.add(key)
        for filename in USER_DATA_FILES:
            source = os.path.join(source_root, filename)
            target = os.path.join(target_root, filename)
            if filename == 'config.json':
                _merge_config_file(
                    source, target, target_root, copied, replaced, conflicts,
                    blocking, warnings)
            else:
                _copy_file_if_missing(source, target, copied, warnings)
        for dirname in USER_DATA_DIRS:
            _merge_directory(
                os.path.join(source_root, dirname),
                os.path.join(target_root, dirname), copied, warnings)
        try:
            browser_dirs = [
                entry for entry in os.scandir(source_root)
                if entry.name.startswith('browser_data') and
                entry.name not in USER_DATA_DIRS and
                entry.is_dir(follow_symlinks=False) and
                not entry.is_symlink()]
        except OSError:
            browser_dirs = []
        for entry in browser_dirs:
            _merge_directory(
                entry.path, os.path.join(target_root, entry.name),
                copied, warnings)

    old_local = legacy_local_root
    if old_local is None:
        old_local = os.path.join(
            os.path.dirname(target_root), LEGACY_APP_DATA_NAME)
    legacy_directories = () if canonical_state_exists else (
            os.path.join('routing', 'providers'),
            os.path.join('routing', 'history'))
    for relative in legacy_directories:
        _merge_directory(
            os.path.join(old_local, relative),
            os.path.join(target_root, relative), copied, warnings)

    bundled = bundled_rule_pack_root or os.path.join(
        resource_root(), 'rule-packs')
    for filename in RULE_PACK_FILES:
        _copy_file_if_missing(
            os.path.join(bundled, filename),
            os.path.join(target_root, 'rule-packs', filename),
            copied, warnings)
    return {
        'data_root': target_root,
        'copied': copied,
        'replaced': replaced,
        'conflicts': conflicts,
        'blocking': blocking,
        'warnings': warnings,
    }
