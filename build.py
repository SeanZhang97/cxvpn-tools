# -*- coding: utf-8 -*-
"""build.py - PyInstaller 打包 CXVPNTools (onedir 便携版)

产物: dist/CXVPNTools/ 整个文件夹可打 zip 分发。
首次构建带控制台窗口便于排错, 稳定后把 --noconsole 打开。
"""
import os
import sys

import PyInstaller.__main__

from build_runtime import build_routing_service
from core.app_paths import migrate_legacy_user_data

BASE = os.path.dirname(os.path.abspath(__file__))
APP_NAME = 'CXVPNTools'
LEGACY_APP_NAMES = ('CX VPN TOOLS', 'CXVPN管理器')
DIST_DIR = os.path.join(BASE, 'dist')
DIST_ROOT = os.path.join(DIST_DIR, APP_NAME)
LEGACY_DIST_ROOTS = [
    os.path.join(DIST_DIR, name) for name in LEGACY_APP_NAMES]
SOURCE_RULE_PACK_DIR = os.path.join(BASE, 'rule-packs')

build_routing_service()

# PyInstaller 会清空 dist；先把旧便携目录中的用户数据复制到 LocalAppData。
legacy_roots = []
for root in (DIST_ROOT, *LEGACY_DIST_ROOTS):
    legacy_roots.extend((root, os.path.join(root, '_internal')))
migration = migrate_legacy_user_data(
    legacy_roots=legacy_roots,
    bundled_rule_pack_root=SOURCE_RULE_PACK_DIR)
if migration['copied']:
    print(f'[build] 已迁移用户数据 {len(migration["copied"])} 个文件')
if migration['replaced']:
    print(f'[build] 已采用较新的旧版配置 {len(migration["replaced"])} 个文件')
if migration['conflicts']:
    preserved = sum(1 for item in migration['conflicts'] if item['backup'])
    print(f'[build] 配置冲突 {len(migration["conflicts"])} 个，'
          f'已保留副本 {preserved} 个')
for warning in migration['warnings']:
    print(f'[build] 用户数据迁移警告: {warning}')
if migration['blocking']:
    raise RuntimeError(
        '[build] 配置冲突副本保存失败，为防止清理旧数据已停止打包')

args = [
    os.path.join(BASE, 'main.py'),
    '--name', APP_NAME,
    '--onedir',
    '--noconfirm',
    '--noconsole',
    '--icon', os.path.join(BASE, 'icon.ico'),
    '--add-data', os.path.join(BASE, 'ui') + os.pathsep + 'ui',
    '--add-data', SOURCE_RULE_PACK_DIR + os.pathsep + 'rule-packs',
    '--add-data', os.path.join(BASE, 'runtime', 'routing') + os.pathsep +
    os.path.join('runtime', 'routing'),
    '--collect-all', 'clr_loader',
    '--collect-submodules', 'webview',
    '--hidden-import', 'webview.platforms.edgechromium',
    '--hidden-import', 'pythonnet',
    '--distpath', os.path.join(BASE, 'dist'),
    '--workpath', os.path.join(BASE, 'build_tmp'),
    '--specpath', BASE,
]
# 稳定后改为 '--noconsole'
PyInstaller.__main__.run(args)
