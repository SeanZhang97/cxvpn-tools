# -*- coding: utf-8 -*-
"""build.py - PyInstaller 打包 CXVPNTools (onedir 便携版)

产物: dist/CXVPNTools/ 整个文件夹可打 zip 分发。
首次构建带控制台窗口便于排错, 稳定后把 --noconsole 打开。
"""
import os
import shutil
import sys

import PyInstaller.__main__

from build_runtime import build_routing_service

BASE = os.path.dirname(os.path.abspath(__file__))
APP_NAME = 'CXVPNTools'
LEGACY_APP_NAMES = ('CX VPN TOOLS', 'CXVPN管理器')
DIST_DIR = os.path.join(BASE, 'dist')
DIST_ROOT = os.path.join(DIST_DIR, APP_NAME)
DIST_CFG = os.path.join(DIST_ROOT, 'config.json')
LEGACY_DIST_ROOTS = [
    os.path.join(DIST_DIR, name) for name in LEGACY_APP_NAMES]
LEGACY_DIST_CFG = [
    os.path.join(root, 'config.json') for root in LEGACY_DIST_ROOTS]
RULE_PACK_FILES = ('local-direct-v1.txt', 'cn-direct-v1.txt')
SOURCE_RULE_PACK_DIR = os.path.join(BASE, 'rule-packs')
DIST_RULE_PACK_DIR = os.path.join(DIST_ROOT, 'rule-packs')

build_routing_service()

# 重打包前保留用户配置, 构建后恢复
saved_cfg = None
source_cfg = next(
    (path for path in (DIST_CFG, *LEGACY_DIST_CFG) if os.path.exists(path)),
    None)
if source_cfg:
    saved_cfg = os.path.join(BASE, 'build_tmp', 'config.json.keep')
    os.makedirs(os.path.dirname(saved_cfg), exist_ok=True)
    shutil.copy2(source_cfg, saved_cfg)
saved_rule_packs = {}
for filename in RULE_PACK_FILES:
    source = next((path for path in (
        os.path.join(DIST_RULE_PACK_DIR, filename),
        *(os.path.join(root, 'rule-packs', filename)
          for root in LEGACY_DIST_ROOTS)) if os.path.isfile(path)), None)
    if source:
        with open(source, 'rb') as stream:
            saved_rule_packs[filename] = stream.read()

args = [
    os.path.join(BASE, 'main.py'),
    '--name', APP_NAME,
    '--onedir',
    '--noconfirm',
    '--noconsole',
    '--icon', os.path.join(BASE, 'icon.ico'),
    '--add-data', os.path.join(BASE, 'ui') + os.pathsep + 'ui',
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

if saved_cfg:
    os.makedirs(DIST_ROOT, exist_ok=True)
    shutil.copy2(saved_cfg, DIST_CFG)
    print('[build] 已恢复用户配置 config.json')
os.makedirs(DIST_RULE_PACK_DIR, exist_ok=True)
for filename in RULE_PACK_FILES:
    target = os.path.join(DIST_RULE_PACK_DIR, filename)
    if filename in saved_rule_packs:
        with open(target, 'wb') as stream:
            stream.write(saved_rule_packs[filename])
    else:
        shutil.copy2(os.path.join(SOURCE_RULE_PACK_DIR, filename), target)
print('[build] 已部署并保留用户规则包 rule-packs')
