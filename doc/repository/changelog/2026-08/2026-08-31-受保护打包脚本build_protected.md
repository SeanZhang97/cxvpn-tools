# 受保护打包脚本 build_protected

- 日期: 2026-08-31
- 功能名: 受保护打包脚本 build_protected.py(Nuitka 编译业务代码防反编译)
- 分支: 非 Git 工作区

## 需求概述

原 build.py 的 PyInstaller 产物保留全部业务字节码, 用解包/反编译工具可轻易还原
源码。在用户要求**不改原 build.py、新增独立脚本**的前提下, 新增
`build_protected.py`: 分发前用 Nuitka 把业务模块编译为本机 .pyd 再交给
PyInstaller, 产物 `dist/CXVPN管理器/` 与前者相同, 但不再携带业务源码与字节码。

## 涉及文件

- `build_protected.py`（新增）
- `AGENTS.md`（增补受保护打包规则, 原自动重打包规则未动）
- `doc/repository/changelog/2026-08/README.md`
- `doc/repository/knowledge/common/打包与分发.md`（增量更新）

## 关键设计

- 编译范围: `api.py` + `core/*.py`（除包 `__init__`）共 16 个模块,
  `nuitka --module` 逐个编译为本机扩展, 按原包结构布署到
  `build_tmp/pyd_stage`, 兼容"裸名"与"点号全名"两种产物命名。
- 独立 spec: 生成 `CXVPN管理器-protected.spec`（不覆盖 build.py 的
  `CXVPN管理器.spec`）; 依赖分析仍基于 .py 源码（第三方/标准库收集与原
  build.py 完全一致, 新增 runtime/routing 数据保留）, 打包时把业务模块的
  pyc 全部剔除、以 .pyd 替换。
- 双断言防裸包: spec 内断言 (1) 业务源码不得以数据文件混入产物;
  (2) 16 个业务 .pyd 必须被完整收集。任一失败立即中止。
- 入口 `main.py` 不参与编译（PyInstaller 入口必须是真实脚本）, 仅窗口装配
  逻辑, 属知情接受的例外。
- 配置保留: 打包前后备份/恢复 `dist/CXVPN管理器/config.json`, 与 build.py
  行为一致; 每次运行重新生成 spec, 编译失败即中止, 绝不回退产出裸包。

## 踩坑记录

- Nuitka 4.2 在 Python 3.13+ 已不支持 `--mingw64`, 必须 MSVC; 本机原本无
  编译器, 通过 winget 安装 VS 2022 Build Tools 最小组件(VC.Tools.x86.x64 +
  Windows11SDK)。Nuitka 的 `--output-dir` 只接受 `--output-dir=` 形式,
  空格分隔形式直接报错退出码 2。
- PyInstaller 的 TOC 条目为 `(dest, src, typecode)` 三元组, dest 在前;
  按直觉按 (src, dest) 解包导致 .pyd 完整性断言永远失败。
- 与另一会话共用 `build_tmp` 存在并发互踩(验证中途 pyd_stage 被清空),
  验证流程改用独立目录 `build_verify/` 完成, 用后已删除。

## 验证情况

- Nuitka 编译 16 模块: 冷编译约 8~12 分钟(单模块 30~60 秒), clcache 热缓存
  下 91 秒; PyInstaller 阶段 18 秒。
- 编译产物 `core/config.cp314-win_amd64.pyd` 以包成员身份导入测试通过:
  `__file__` 路径推导、config 读写往返均正常。
- 隔离构建(输出到独立目录, 未触碰 dist/未杀进程)端到端通过: spec 内断言全过;
  深检确认 exe 内嵌 PYZ 与 `base_library.zip` 均无业务字节码, 16 个业务 .pyd
  全部落位, 第三方目录 ui/webview/clr_loader/numpy/PIL/pythonnet 与
  runtime/routing 齐全。
- 未执行项: 运行态冒烟与正式重打包(dist 未动)——已有另一会话运行的实例
  (PID 840) 在跑, 按用户"另一个会话正在改功能, 注意一下"的指示未做任何
  杀进程/重启操作。

## 注意事项

- 日常迭代仍默认 `build.py`(快、不保护); 对外交付用
  `uv run python build_protected.py`。
- 正式启用受保护打包需要一次完整 `build_protected.py` 运行(冷编译约 10 分钟),
  建议在另一会话功能联调结束后由用户安排执行。