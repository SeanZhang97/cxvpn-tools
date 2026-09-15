# -*- coding: utf-8 -*-
"""Build the pinned upstream core with the reviewed VPN hook; no network during normal packaging."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile

BASE = Path(__file__).resolve().parent
PATCH = BASE / 'patches' / 'mihomo'
UPSTREAM = 'v1.19.30'
VERSION = UPSTREAM + '-cxvpn.2'
SOURCE_SHA256 = '6790545B467FD6E61B6610793F37B64FAC7F59B061F222501F6CC3A7A5DE7C8B'
SOURCE_URL = f'https://codeload.github.com/MetaCubeX/mihomo/zip/refs/tags/{UPSTREAM}'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest().upper()


def patch_digest():
    h = hashlib.sha256()
    for path in sorted([PATCH / 'apply.py', *(PATCH / 'overlay').rglob('*.go')]):
        h.update(path.relative_to(PATCH).as_posix().encode('utf-8'))
        h.update(path.read_text(encoding='utf-8').replace('\r\n', '\n').encode('utf-8'))
    return h.hexdigest().upper()


def verify_installed():
    manifest = json.loads((BASE / 'runtime/routing/mihomo-build.json').read_text(encoding='utf-8'))
    if (manifest['version'] != VERSION or manifest['source_sha256'] != SOURCE_SHA256
            or manifest['patch_sha256'] != patch_digest()
            or manifest['binary_sha256'] != digest(BASE / 'runtime/routing/mihomo.exe')
            or manifest['corresponding_source_sha256'] != digest(BASE / 'runtime/routing/mihomo-source.zip')):
        raise RuntimeError('定制 Mihomo 与补丁不一致，请先运行 python build_mihomo.py --install')


def build(install=False):
    go = shutil.which('go')
    if not go:
        raise RuntimeError('缺少 Go 构建工具')
    cache = BASE / 'build_tmp' / 'mihomo-custom'
    cache.mkdir(parents=True, exist_ok=True)
    source = BASE / 'build_tmp' / f'mihomo-{UPSTREAM}.zip'
    if not source.exists():
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(SOURCE_URL, timeout=30) as response:
            data = response.read(32 * 1024 * 1024 + 1)
        if len(data) > 32 * 1024 * 1024:
            raise RuntimeError('上游源码归档超出大小限制')
        source.write_bytes(data)
    if digest(source) != SOURCE_SHA256:
        raise RuntimeError('上游源码归档 SHA-256 不匹配')
    env = os.environ.copy()
    env.update(GOOS='windows', GOARCH='amd64', GOAMD64='v1', CGO_ENABLED='0')
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY'):
        env.pop(key, None)
    with tempfile.TemporaryDirectory(prefix='source-', dir=cache) as temp:
        root = Path(temp).resolve()
        with zipfile.ZipFile(source) as archive:
            for name in archive.namelist():
                if not (root / name).resolve().is_relative_to(root):
                    raise RuntimeError('上游源码归档路径无效')
            archive.extractall(root)
        tree = root / 'mihomo-1.19.30'
        spec = importlib.util.spec_from_file_location('mihomo_patch', PATCH / 'apply.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.apply(tree)
        output = cache / 'mihomo.exe'
        # Fixed metadata and trimpath make the source/patch/toolchain combination repeatable.
        flags = ('-w -s -buildid= -X github.com/metacubex/mihomo/constant.Version=' + VERSION
                 + ' -X github.com/metacubex/mihomo/constant.BuildTime=2026-09-14')
        subprocess.run([go, 'test', '-timeout=45s', './component/vpnroute', './component/dialer'],
                       cwd=tree, env=env, check=True, timeout=60)
        subprocess.run([go, 'test', '-timeout=45s', '-run', '^TestCXVPNDirectCompatibility$', './adapter'],
                       cwd=tree, env=env, check=True, timeout=60)
        subprocess.run([go, 'build', '-tags', 'with_gvisor', '-trimpath', '-buildvcs=false',
                        '-ldflags', flags, '-o', str(output), '.'],
                       cwd=tree, env=env, check=True, timeout=120)
        (tree / 'CXVPN-BUILD.txt').write_text(
            'Upstream: ' + UPSTREAM + '\nCore version: ' + VERSION
            + '\nGOOS=windows GOARCH=amd64 GOAMD64=v1 CGO_ENABLED=0\n'
            + 'go build -tags with_gvisor -trimpath -buildvcs=false -ldflags "'
            + flags + '" -o mihomo.exe .\n', encoding='utf-8')
        with zipfile.ZipFile(cache / 'mihomo-source.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(tree.rglob('*')):
                if path.is_file():
                    info = zipfile.ZipInfo(path.relative_to(tree).as_posix(), date_time=(2026,9,14,0,0,0))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    archive.writestr(info, path.read_bytes())
    compiler = subprocess.check_output([go, 'version'], text=True, encoding='utf-8',
                                       errors='backslashreplace', timeout=5).strip()
    manifest = {'version': VERSION, 'upstream': UPSTREAM, 'source_url': SOURCE_URL,
                'source_sha256': SOURCE_SHA256, 'patch_sha256': patch_digest(),
                'binary_sha256': digest(output), 'compiler': compiler,
                'corresponding_source_sha256': digest(cache / 'mihomo-source.zip'),
                'goamd64': 'v1', 'tags': ['with_gvisor'], 'capability': 2}
    (cache / 'mihomo-build.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    if install:
        runtime = BASE / 'runtime/routing'
        backup = cache / ('mihomo-before-' + digest(runtime / 'mihomo.exe')[:12] + '.exe')
        if not backup.exists():
            shutil.copy2(runtime / 'mihomo.exe', backup)
        shutil.copy2(output, runtime / 'mihomo.exe')
        shutil.copy2(cache / 'mihomo-build.json', runtime / 'mihomo-build.json')
        shutil.copy2(cache / 'mihomo-source.zip', runtime / 'mihomo-source.zip')
        for relative, pattern, replacement in [
            ('core/routing.py', r"MIHOMO_SHA256 = '[0-9A-F]+'", f"MIHOMO_SHA256 = '{manifest['binary_sha256']}'"),
            ('routing-service/src/constants.rs', r'pub const MIHOMO_SHA256: &str = "[0-9A-F]+";',
             f'pub const MIHOMO_SHA256: &str = "{manifest["binary_sha256"]}";')]:
            path = BASE / relative
            text, count = re.subn(pattern, replacement, path.read_text(encoding='utf-8'))
            if count != 1:
                raise RuntimeError('运行时摘要常量匹配失败: ' + relative)
            path.write_text(text, encoding='utf-8')
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--install', action='store_true')
    build(parser.parse_args().install)
