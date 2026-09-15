# -*- coding: utf-8 -*-
"""构建并布署随主程序分发的原生运行时。"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess


BASE = os.path.dirname(os.path.abspath(__file__))
SERVICE_ROOT = os.path.join(BASE, 'routing-service')
SERVICE_TARGET = os.path.join(
    SERVICE_ROOT, 'target', 'release', 'cxvpn-routing-service.exe')
SERVICE_RUNTIME = os.path.join(
    BASE, 'runtime', 'routing', 'CXVPNRoutingHost.exe')


def build_routing_service(force=False):
    from build_mihomo import verify_installed
    verify_installed()
    sources = [
        os.path.join(SERVICE_ROOT, 'Cargo.toml'),
        os.path.join(SERVICE_ROOT, 'Cargo.lock'),
        *glob.glob(os.path.join(SERVICE_ROOT, 'src', '*.rs')),
    ]
    newest_source = max(
        (os.path.getmtime(path) for path in sources if os.path.isfile(path)),
        default=0)
    if (not force and os.path.isfile(SERVICE_RUNTIME) and
            os.path.getmtime(SERVICE_RUNTIME) >= newest_source):
        print('[build] 原生路由服务已是最新版本')
        return SERVICE_RUNTIME

    cargo = shutil.which('cargo')
    if not cargo:
        candidate = os.path.join(
            os.environ.get('CARGO_HOME') or os.path.join(os.path.expanduser('~'), '.cargo'),
            'bin', 'cargo.exe')
        cargo = candidate if os.path.isfile(candidate) else ''
    if not cargo:
        raise RuntimeError(
            '缺少 Rust cargo，无法构建 CXVPNRoutingHost.exe；请先安装 rustup')
    subprocess.run([
        cargo, 'build', '--release', '--manifest-path',
        os.path.join(SERVICE_ROOT, 'Cargo.toml'),
    ], cwd=BASE, check=True)
    if not os.path.isfile(SERVICE_TARGET):
        raise RuntimeError('Rust 构建完成但未找到路由服务产物')
    os.makedirs(os.path.dirname(SERVICE_RUNTIME), exist_ok=True)
    shutil.copy2(SERVICE_TARGET, SERVICE_RUNTIME)
    print('[build] 已更新原生路由服务 CXVPNRoutingHost.exe')
    return SERVICE_RUNTIME
