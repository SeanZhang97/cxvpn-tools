# -*- coding: utf-8 -*-
"""按输入内容复用 Nuitka 扩展；未完成或损坏的缓存绝不进入分发产物。"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import time


OPTIONS = ('--module', '--no-pyi-file')
COMPILE_TIMEOUT = 1200


def digest_file(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def emit(message):
    """控制台编码失败不得破坏构建或清理。"""
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, 'encoding', None) or 'utf-8'
        try:
            print(message.encode(encoding, 'backslashreplace').decode(encoding), flush=True)
        except (OSError, ValueError):
            pass
    except (OSError, ValueError):
        pass


def environment_identity():
    compiler = shutil.which('cl.exe')
    toolchains = {}
    # 普通 PowerShell 中 cl 不在 PATH，Nuitka 会自行发现 VS；仍需捕捉工具链更新。
    visual_studio = Path(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')) / 'Microsoft Visual Studio' / 'Installer' / 'vswhere.exe'
    if visual_studio.is_file():
        result = subprocess.run([str(visual_studio), '-products', '*', '-format', 'json', '-utf8'],
                                check=True, capture_output=True, text=True,
                                encoding='utf-8-sig', errors='strict', timeout=15)
        for installation in json.loads(result.stdout):
            install_root = Path(installation['installationPath'])
            toolchains[str(install_root)] = installation.get('installationVersion', '')
            for binary in install_root.glob('VC/Tools/MSVC/*/bin/Hostx64/x64/cl.exe'):
                toolchains[str(binary)] = digest_file(binary)
    return {
        'schema': 1,
        'python': sys.version,
        'executable': str(Path(sys.executable).resolve()),
        'abi': sysconfig.get_config_var('SOABI'),
        'extension': sysconfig.get_config_var('EXT_SUFFIX'),
        'machine': platform.machine(),
        'nuitka': importlib.metadata.version('nuitka'),
        'options': OPTIONS,
        'compiler': (compiler, digest_file(compiler)) if compiler else None,
        'visual_studio': toolchains,
        'toolchain_env': {name: os.environ.get(name, '') for name in
                          ('VCToolsVersion', 'WindowsSDKVersion', 'CC', 'CFLAGS', 'CL')},
    }


def cache_key(source, module, identity):
    source = Path(source).resolve()
    packages = {}
    parent = source.parent
    while (parent / '__init__.py').is_file():
        packages[str(parent / '__init__.py')] = digest_file(parent / '__init__.py')
        parent = parent.parent
    payload = {'source': str(source), 'sha256': digest_file(source),
               'module': module, 'packages': packages, 'environment': identity}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode('utf-8')).hexdigest()


def read_cached(folder):
    try:
        record = json.loads((folder / 'complete.json').read_text(encoding='utf-8'))
        name = record['name']
        if Path(name).name != name or not name.endswith('.pyd'):
            return None
        binary = folder / name
        if binary.is_file() and digest_file(binary) == record['sha256']:
            return binary
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def compile_cached(source, module, cache_root, identity, runner=subprocess.run):
    """每模块独立编译，只有成功产物及摘要一起完成后才成为缓存。"""
    source = Path(source).resolve()
    root = Path(cache_root).resolve()
    folder = root / module / cache_key(source, module, identity)
    cached = read_cached(folder)
    if cached is not None:
        emit(f'[build-protected] 缓存命中: {module}')
        return cached
    folder.mkdir(parents=True, exist_ok=True)
    # 临时目录只包含本次生成的编译输出，不清空其它模块或历史内容缓存。
    with tempfile.TemporaryDirectory(prefix='compile-', dir=folder) as temporary:
        emit(f'[build-protected] 开始编译: {module}; timeout={COMPILE_TIMEOUT}s')
        started = time.monotonic()
        try:
            runner([sys.executable, '-m', 'nuitka', *OPTIONS,
                    '--output-dir=' + temporary, str(source)],
                   check=True, timeout=COMPILE_TIMEOUT)
        except Exception as exc:
            emit(f'[build-protected] 编译失败: {module}; type={type(exc).__name__}; '
                 f'elapsed={time.monotonic() - started:.1f}s')
            raise
        tail = module.rsplit('.', 1)[-1]
        candidates = sorted({path for path in Path(temporary).glob('*.pyd')
                             if path.name.startswith((tail + '.', module + '.'))})
        if len(candidates) != 1:
            raise RuntimeError(f'业务模块 {module} 编译产物数量异常: {len(candidates)}')
        binary = candidates[0]
        suffix = binary.name[len(module if binary.name.startswith(module + '.') else tail):]
        target = folder / (tail + suffix)
        record = {'name': target.name, 'sha256': digest_file(binary)}
        os.replace(binary, target)
        manifest = Path(temporary) / 'complete.json'
        manifest.write_text(json.dumps(record, sort_keys=True), encoding='utf-8')
        os.replace(manifest, folder / 'complete.json')
    emit(f'[build-protected] 编译完成: {module}; elapsed={time.monotonic() - started:.1f}s')
    return target
