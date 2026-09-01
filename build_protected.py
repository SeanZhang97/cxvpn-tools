# -*- coding: utf-8 -*-
"""build_protected.py - 受保护打包(业务代码 Nuitka 编译为 .pyd + PyInstaller)

与 build.py 并存、互不修改:build.py 是原打包流程(不保护业务代码),
本脚本在其基础上把业务模块编译为本机扩展 .pyd 再交给 PyInstaller,
分发后无法用解包/反编译工具还原业务源码。

流程:
1. Nuitka --module 把 api.py 与 core/*.py(除包 __init__)编译为 .pyd;
2. 生成 CX VPN TOOLS-protected.spec(独立文件名, 不与 build.py 的 spec 冲突):
   - 依赖分析仍基于 .py 源码, 第三方/标准库收集与 build.py 完全一致;
   - 打包输出时把业务模块的 pyc 全部替换为第 1 步的 .pyd, 产物内不留业务源码;
   - spec 内断言校验, 残留源码或 .pyd 缺失时直接中止, 绝不静默产出裸包。
3. 产物与 build.py 相同: dist/CX VPN TOOLS/(onedir 便携目录), 打包前后
   自动备份/恢复用户配置。

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

import PyInstaller.__main__

from build_runtime import build_routing_service

BASE = os.path.dirname(os.path.abspath(__file__))
APP_NAME = 'CX VPN TOOLS'
LEGACY_APP_NAME = 'CXVPN管理器'
DIST_DIR = os.path.join(BASE, 'dist')
DIST_ROOT = os.path.join(DIST_DIR, APP_NAME)
DIST_CFG = os.path.join(DIST_ROOT, 'config.json')
LEGACY_DIST_CFG = os.path.join(
    DIST_DIR, LEGACY_APP_NAME, 'config.json')
RULE_PACK_FILES = ('local-direct-v1.txt', 'cn-direct-v1.txt')
SOURCE_RULE_PACK_DIR = os.path.join(BASE, 'rule-packs')
DIST_RULE_PACK_DIR = os.path.join(DIST_ROOT, 'rule-packs')
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
datas += [(@@UI_SRC@@, 'ui'), (@@RT_SRC@@, 'runtime/routing')]

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
          entitlements_file=None, icon=[@@ICON@@])
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=True,
               upx_exclude=[], name=@@APP_NAME@@)
'''


def write_spec(staged):
    spec = (_SPEC_TEMPLATE
            .replace('@@BASE@@', repr(BASE))
            .replace('@@APP_NAME@@', repr(APP_NAME))
            .replace('@@STAGED@@', repr(staged))
            .replace('@@MAIN_SRC@@', repr(os.path.join(BASE, 'main.py')))
            .replace('@@UI_SRC@@', repr(os.path.join(BASE, 'ui')))
            .replace('@@RT_SRC@@', repr(os.path.join(BASE, 'runtime',
                                                     'routing')))
            .replace('@@ICON@@', repr(os.path.join(BASE, 'icon.ico'))))
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
    build_routing_service()
    saved_cfg = None
    source_cfg = next(
        (path for path in (DIST_CFG, LEGACY_DIST_CFG)
         if os.path.exists(path)), None)
    if source_cfg:
        saved_cfg = os.path.join(WORK, 'config.json.keep')
        os.makedirs(WORK, exist_ok=True)
        shutil.copy2(source_cfg, saved_cfg)
    saved_rule_packs = {}
    for filename in RULE_PACK_FILES:
        path = os.path.join(DIST_RULE_PACK_DIR, filename)
        if os.path.isfile(path):
            with open(path, 'rb') as stream:
                saved_rule_packs[filename] = stream.read()
    try:
        staged = compile_business_code()
        write_spec(staged)
        PyInstaller.__main__.run(
            [SPEC_PATH, '--noconfirm',
             '--distpath', DIST_DIR, '--workpath', WORK])
        verify_artifacts(staged)
    finally:
        if saved_cfg and os.path.exists(saved_cfg):
            os.makedirs(DIST_ROOT, exist_ok=True)
            shutil.copy2(saved_cfg, DIST_CFG)
            print('[build-protected] 已恢复用户配置 config.json')
        os.makedirs(DIST_RULE_PACK_DIR, exist_ok=True)
        for filename in RULE_PACK_FILES:
            target = os.path.join(DIST_RULE_PACK_DIR, filename)
            if filename in saved_rule_packs:
                with open(target, 'wb') as stream:
                    stream.write(saved_rule_packs[filename])
            else:
                shutil.copy2(
                    os.path.join(SOURCE_RULE_PACK_DIR, filename), target)
        print('[build-protected] 已部署并保留用户规则包 rule-packs')
    print('[build-protected] 完成: %s'
          % DIST_ROOT)


if __name__ == '__main__':
    main()
