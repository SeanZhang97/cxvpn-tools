# -*- coding: utf-8 -*-
"""build_protected.py - 受保护打包(业务代码 Nuitka 编译为 .pyd + PyInstaller)

与 build.py 并存、互不修改:build.py 是原打包流程(不保护业务代码),
本脚本在其基础上把业务模块编译为本机扩展 .pyd 再交给 PyInstaller,
分发后无法用解包/反编译工具还原业务源码。

流程:
1. Nuitka --module 把 api.py 与 core/*.py(除包 __init__)编译为 .pyd;
2. 生成 CXVPNTools-protected.spec(独立文件名, 不与 build.py 的 spec 冲突):
   - 依赖分析仍基于 .py 源码, 第三方/标准库收集与 build.py 完全一致;
   - 打包输出时把业务模块的 pyc 全部替换为第 1 步的 .pyd, 产物内不留业务源码;
   - spec 内断言校验, 残留源码或 .pyd 缺失时直接中止, 绝不静默产出裸包。
3. 产物与 build.py 相同: dist/CXVPNTools/(onedir 目录)，用户数据写入
   当前用户 LocalAppData，打包前先迁移旧便携目录数据。

唯一仍以字节码随包的业务文件是入口 main.py(PyInstaller 入口必须是真实脚本),
其中只有窗口装配代码, 不含业务逻辑。

依赖与首次准备:
- 依赖 Nuitka 与 MSVC 工具链: uv pip install nuitka
  winget install --id Microsoft.VisualStudio.2022.BuildTools -e ^
    --override "--wait --quiet --norestart --nocache ^
    --add Microsoft.VisualStudio.Component.VC.Tools.x86.x64 ^
    --add Microsoft.VisualStudio.Component.Windows11SDK.26100"
- 用法: uv run python build_protected.py
"""
import glob
import os
import shutil
import subprocess
import sys

from core.desktop_runtime import ensure_desktop_runtime

ensure_desktop_runtime(wait=True)

import PyInstaller.__main__

from build_runtime import build_routing_service
from build_lifecycle import complete_build, prepare_build
from core.app_paths import migrate_legacy_user_data
from core.version import APP_VERSION, APP_VERSION_TUPLE

BASE = os.path.dirname(os.path.abspath(__file__))
APP_NAME = 'CXVPNTools'
LEGACY_APP_NAMES = ('CX VPN TOOLS', 'CXVPN管理器')
DIST_DIR = os.path.join(BASE, 'dist')
DIST_ROOT = os.path.join(DIST_DIR, APP_NAME)
LEGACY_DIST_ROOTS = [
    os.path.join(DIST_DIR, name) for name in LEGACY_APP_NAMES]
SOURCE_RULE_PACK_DIR = os.path.join(BASE, 'rule-packs')
WORK = os.path.join(BASE, 'build_tmp')
NUITKA_OUT = os.path.join(WORK, 'nuitka_out')
PYD_STAGE = os.path.join(WORK, 'pyd_stage')
SPEC_PATH = os.path.join(BASE, APP_NAME + '-protected.spec')

# 参与编译的业务模块: api + core 下除 __init__ 外全部 .py
PROTECT_SRC = [os.path.join(BASE, 'api.py')] + sorted(
    p for p in glob.glob(os.path.join(BASE, 'core', '*.py'))
    if os.path.basename(p) != '__init__.py')


def module_name(src):
    if src == os.path.join(BASE, 'api.py'):
        return 'api'
    return 'core.' + os.path.splitext(os.path.basename(src))[0]


def compile_business_code():
    """编译业务模块为 .pyd 并按原包结构布到 build_tmp/pyd_stage。

    返回 {模块名: 布署后的 pyd 绝对路径}。任一模块编译失败即中止,
    不允许退回成未保护源码继续打包。
    """
    shutil.rmtree(NUITKA_OUT, ignore_errors=True)
    shutil.rmtree(PYD_STAGE, ignore_errors=True)
    os.makedirs(NUITKA_OUT, exist_ok=True)
    os.makedirs(PYD_STAGE, exist_ok=True)
    for src in PROTECT_SRC:
        print('[build-protected] Nuitka 编译', os.path.relpath(src, BASE))
        subprocess.run(
            [sys.executable, '-m', 'nuitka', '--module', '--no-pyi-file',
             '--output-dir=' + NUITKA_OUT, src],
            check=True)
    staged = {}
    for src in PROTECT_SRC:
        mod = module_name(src)
        tail = mod.rsplit('.', 1)[-1]
        # 兼容点号全名(core.config.*.pyd)与裸名(config.*.pyd)两种产物命名
        cands = sorted(set(
            glob.glob(os.path.join(NUITKA_OUT, '**', mod + '.*.pyd'),
                      recursive=True)
            + glob.glob(os.path.join(NUITKA_OUT, '**', tail + '.*.pyd'),
                        recursive=True)))
        if len(cands) != 1:
            raise RuntimeError(
                '业务模块 %s 编译产物定位失败(找到 %d 个): %s'
                % (mod, len(cands), cands))
        pyd = cands[0]
        suffix = os.path.basename(pyd)[len(tail):]
        dest_dir = PYD_STAGE if mod == 'api' \
            else os.path.join(PYD_STAGE, 'core')
        os.makedirs(dest_dir, exist_ok=True)
        target = os.path.join(dest_dir, tail + suffix)
        shutil.copy2(pyd, target)
        staged[mod] = target
    return staged


_SPEC_TEMPLATE = '''# -*- mode: python ; coding: utf-8 -*-
# 本文件由 build_protected.py 自动生成, 勿手工编辑
import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

BASE = @@BASE@@
STAGED = @@STAGED@@

binaries = [(p, ('core' if m.startswith('core.') else '.'))
            for m, p in STAGED.items()]
datas = []
hiddenimports = ['webview.platforms.edgechromium', 'pythonnet']
hiddenimports += collect_submodules('webview')
tmp = collect_all('clr_loader')
datas += tmp[0]; binaries += tmp[1]; hiddenimports += tmp[2]
datas += [(@@UI_SRC@@, 'ui'), (@@RULES_SRC@@, 'rule-packs'),
          (@@RT_SRC@@, 'runtime/routing')]

PROTECT = frozenset(STAGED)

a = Analysis([@@MAIN_SRC@@], pathex=[BASE], binaries=binaries, datas=datas,
             hiddenimports=hiddenimports, hookspath=[],
             hooksconfig={}, runtime_hooks=[], excludes=[],
             noarchive=False, optimize=0)

a.pure = [t for t in a.pure if t[0] not in PROTECT]

src_roots = (os.path.join(BASE, 'core'), os.path.join(BASE, 'api.py'))
shipped = [(s, d) for d, s, _t in a.datas + a.binaries
           if s == src_roots[1] or s.startswith(src_roots[0] + os.sep)]
assert not shipped, '打包中止: 业务源码以数据文件混入产物: %s' % shipped

got = {m for m, p in STAGED.items()
       for _d, src, _t in a.binaries
       if os.path.normcase(src) == os.path.normcase(p)}
assert got == set(STAGED), '打包中止: 业务 .pyd 未被完整收集, 缺 %s' % \
    sorted(set(STAGED) - got)

pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=@@APP_NAME@@,
          debug=False, bootloader_ignore_signals=False, strip=False,
          upx=True, console=False, disable_windowed_traceback=False,
          argv_emulation=False, target_arch=None, codesign_identity=None,
          entitlements_file=None, icon=[@@ICON@@], version=@@VERSION_FILE@@)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=True,
               upx_exclude=[], name=@@APP_NAME@@)
'''


def write_spec(staged):
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo, StringFileInfo, StringStruct, StringTable,
        VarFileInfo, VarStruct, VSVersionInfo)
    version_file = os.path.join(WORK, 'protected-version.txt')
    version = VSVersionInfo(
        ffi=FixedFileInfo(filevers=APP_VERSION_TUPLE,
                          prodvers=APP_VERSION_TUPLE, fileType=1),
        kids=[StringFileInfo([StringTable('040904B0', [
            StringStruct('CompanyName', APP_NAME),
            StringStruct('FileDescription', APP_NAME),
            StringStruct('FileVersion', '.'.join(map(str, APP_VERSION_TUPLE))),
            StringStruct('InternalName', APP_NAME + '.exe'),
            StringStruct('OriginalFilename', APP_NAME + '.exe'),
            StringStruct('ProductName', APP_NAME),
            StringStruct('ProductVersion', APP_VERSION),
        ])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])])
    os.makedirs(WORK, exist_ok=True)
    with open(version_file, 'w', encoding='utf-8') as stream:
        stream.write(str(version))
    spec = (_SPEC_TEMPLATE
            .replace('@@BASE@@', repr(BASE))
            .replace('@@APP_NAME@@', repr(APP_NAME))
            .replace('@@STAGED@@', repr(staged))
            .replace('@@MAIN_SRC@@', repr(os.path.join(BASE, 'main.py')))
            .replace('@@UI_SRC@@', repr(os.path.join(BASE, 'ui')))
            .replace('@@RULES_SRC@@', repr(SOURCE_RULE_PACK_DIR))
            .replace('@@RT_SRC@@', repr(os.path.join(BASE, 'runtime',
                                                     'routing')))
            .replace('@@ICON@@', repr(os.path.join(BASE, 'icon.ico')))
            .replace('@@VERSION_FILE@@', repr(version_file)))
    with open(SPEC_PATH, 'w', encoding='utf-8') as f:
        f.write(spec)


def verify_artifacts(staged):
    """产物完整性: .pyd 落位 + exe 内嵌归档与 base_library.zip 无业务字节码。

    main.py 例外: PyInstaller 入口脚本必然以字节码内嵌 exe, 属预期。
    """
    dist_root = DIST_ROOT
    internal = os.path.join(dist_root, '_internal')
    for mod in sorted(staged):
        tail = mod.rsplit('.', 1)[-1]
        hits = glob.glob(os.path.join(internal,
                                      'core', tail + '.*.pyd')) \
            if mod != 'api' else glob.glob(
                os.path.join(internal, tail + '.*.pyd'))
        assert len(hits) == 1, '产物缺少 %s 的 .pyd' % mod

    from PyInstaller.archive.readers import CArchiveReader
    import zipfile
    protect = set(staged)
    arch = CArchiveReader(os.path.join(dist_root, APP_NAME + '.exe'))
    pyz = arch.open_embedded_archive('PYZ.pyz')
    in_exe = protect & set(pyz.toc)
    assert not in_exe, 'exe 内嵌 PYZ 残留业务模块: %s' % sorted(in_exe)
    with zipfile.ZipFile(os.path.join(internal, 'base_library.zip')) as z:
        in_bl = protect & {n[:-4].replace('/', '.')
                           for n in z.namelist() if n.endswith('.pyc')}
    assert not in_bl, 'base_library.zip 残留业务模块: %s' % sorted(in_bl)


def main():
    user_config_snapshot = prepare_build()
    build_routing_service()
    legacy_roots = []
    for root in (
            DIST_ROOT, *LEGACY_DIST_ROOTS,
            *user_config_snapshot.get('running_roots', [])):
        legacy_roots.extend((root, os.path.join(root, '_internal')))
    migration = migrate_legacy_user_data(
        legacy_roots=legacy_roots,
        bundled_rule_pack_root=SOURCE_RULE_PACK_DIR)
    if migration['copied']:
        print('[build-protected] 已迁移用户数据 %d 个文件'
              % len(migration['copied']))
    if migration['replaced']:
        print('[build-protected] 已采用较新的旧版配置 %d 个文件'
              % len(migration['replaced']))
    if migration['conflicts']:
        preserved = sum(
            1 for item in migration['conflicts'] if item['backup'])
        print('[build-protected] 配置冲突 %d 个，已保留副本 %d 个'
              % (len(migration['conflicts']), preserved))
    for warning in migration['warnings']:
        print('[build-protected] 用户数据迁移警告: %s' % warning)
    if migration['blocking']:
        raise RuntimeError(
            '[build-protected] 配置冲突副本保存失败，'
            '为防止清理旧数据已停止打包')
    staged = compile_business_code()
    write_spec(staged)
    PyInstaller.__main__.run(
        [SPEC_PATH, '--noconfirm',
         '--distpath', DIST_DIR, '--workpath', WORK])
    verify_artifacts(staged)
    complete_build(user_config_snapshot, DIST_ROOT, APP_NAME)
    print('[build-protected] 完成: %s'
          % DIST_ROOT)


if __name__ == '__main__':
    main()
