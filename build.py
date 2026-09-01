# -*- coding: utf-8 -*-
"""build.py - PyInstaller 打包 (onedir 便携版)

产物: dist/CXVPN管理器/ 整个文件夹可打 zip 分发。
首次构建带控制台窗口便于排错, 稳定后把 --noconsole 打开。
"""
import os
import shutil
import sys

import PyInstaller.__main__

from build_runtime import build_routing_service

BASE = os.path.dirname(os.path.abspath(__file__))
DIST_CFG = os.path.join(BASE, 'dist', 'CXVPN管理器', 'config.json')

build_routing_service()

# 重打包前保留用户配置, 构建后恢复
saved_cfg = None
if os.path.exists(DIST_CFG):
    saved_cfg = os.path.join(BASE, 'build_tmp', 'config.json.keep')
    os.makedirs(os.path.dirname(saved_cfg), exist_ok=True)
    shutil.copy2(DIST_CFG, saved_cfg)

args = [
    os.path.join(BASE, 'main.py'),
    '--name', 'CXVPN管理器',
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
    shutil.copy2(saved_cfg, DIST_CFG)
    print('[build] 已恢复用户配置 config.json')
