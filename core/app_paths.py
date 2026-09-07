# -*- coding: utf-8 -*-
"""应用资源目录、当前用户数据目录与旧版数据迁移。"""
from __future__ import annotations

import os
import shutil
import sys


APP_NAME = 'CXVPNTools'
LEGACY_APP_DATA_NAME = 'CXVPNManager'
USER_DATA_FILES = (
    'config.json',
    'run.log',
    'startup.log',
    'netlog.jsonl',
    'proxy_guard.json',
    'proxy_guard_snapshot.json',
)
USER_DATA_DIRS = (
    'webview_data',
    'browser_data',
    'captcha_cache',
    'routing_data',
    'rule-packs',
)
RULE_PACK_FILES = ('local-direct-v1.txt', 'cn-direct-v1.txt')


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
    """把便携目录和旧产品 LocalAppData 数据合并到当前用户目录。

    迁移只复制目标中不存在的文件，不删除旧数据，也不跟随符号链接。
    """
    target_root = os.path.abspath(data_root or user_data_root())
    copied = []
    warnings = []
    try:
        os.makedirs(target_root, exist_ok=True)
    except OSError as exc:
        return {
            'data_root': target_root,
            'copied': copied,
            'warnings': [f'{target_root}: {type(exc).__name__}: {exc}'],
        }

    if legacy_roots is None:
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
            _copy_file_if_missing(
                os.path.join(source_root, filename),
                os.path.join(target_root, filename), copied, warnings)
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
    for relative in (
            os.path.join('routing', 'providers'),
            os.path.join('routing', 'history')):
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
    return {'data_root': target_root, 'copied': copied, 'warnings': warnings}
