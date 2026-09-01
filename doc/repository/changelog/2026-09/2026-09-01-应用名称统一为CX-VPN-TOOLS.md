# 应用名称统一为 CX VPN TOOLS

- 日期：2026-09-01
- 功能名：应用品牌名称统一
- 分支：非 Git 工作区

## 需求概述与问题根因

主窗口、系统托盘和页面标识同时使用“CX VPN 管理器”与“CX VPN MANAGER”，
品牌名称不一致。本次统一为“CX VPN TOOLS”。

## 涉及文件

- `main.py`
- `build.py`
- `build_protected.py`
- `打包纯净版.bat`
- `AGENTS.md`
- `core/windows_desktop.py`
- `ui/index.html`
- `ui/style.css`
- `tests/test_ui_polish.js`
- `tests/test_build_branding.py`
- `doc/repository/README.md`
- `doc/repository/knowledge/INDEX.md`
- `doc/repository/knowledge/modules/CXVPN管理器.md`
- `doc/repository/changelog/2026-09/README.md`

## 关键设计

- 主窗口复用 `core/windows_desktop.py` 的 `WINDOW_TITLE`，避免窗口标题与单实例唤醒目标再次分叉。
- 系统托盘提示和退出菜单复用同一名称常量。
- HTML 页面标题与侧栏品牌标识同步更名。
- 侧栏品牌区改为单行组合：`CX VPN` 与青色弱调 `TOOLS` 标签同基线排列，
  并统一图标尺寸、圆角、间距和垂直居中，解决原两行字号与字距失衡。
- 普通打包、受保护打包和纯净分发包的目录、exe、spec 与 zip 名称统一为
  `CX VPN TOOLS`。新目录首次生成时从旧产物迁移 `config.json`。
- 单实例互斥名和本地路由数据目录继续使用稳定的内部 ID `CXVPNManager`，
  避免更名后启动重复实例或丢失既有本地配置。

## 影响范围

影响主窗口标题、托盘名称与菜单、HTML 标题、侧栏品牌字样、打包目录、exe、
spec 和纯净 zip 名称；不改变业务逻辑、单实例互斥名或本地路由数据目录。

## 验证情况

- `uv run python -m py_compile main.py core/windows_desktop.py`：通过。
- `uv run python -m unittest tests.test_windows_desktop`：12 个测试通过。
- `node --check ui/app.js` 与 `node tests/test_ui_polish.js`：通过，覆盖页面标题、
  侧栏单行品牌结构、`TOOLS` 标签样式、窗口标题常量与旧名称清理。
- `uv run python -m py_compile build.py build_protected.py tests/test_build_branding.py`：通过。
- `uv run python -m unittest tests.test_build_branding`：3 个测试通过，覆盖普通打包、
  受保护打包、纯净 zip 名称和旧配置迁移边界。
- 本地浏览器按 216px 侧栏实际渲染检查：品牌区无裁切，44px 图标与文字垂直居中，
  文字组合宽约 110px，`CX VPN` 与 `TOOLS` 同基线显示。
- `uv run --with pyinstaller python build.py`：通过，产物为
  `dist/CX VPN TOOLS/CX VPN TOOLS.exe`，exe 时间戳为
  2026-09-01 18:46:15。
- 首次更名打包已从旧目录迁移 `config.json`；迁移前后均为 3609 字节，SHA-256 均为
  `DDB595B2D42811DB0BC92275E9693698410390784A680A7067327E6B4CC531C2`，用户配置未丢失。
- `dist` 中已只保留 `CX VPN TOOLS` 产物目录；旧目录、zip 和 spec 移入
  `build_tmp/legacy-brand-artifacts/` 作可恢复备份。
- 打包内 `_internal/ui/index.html` 已包含 `CX VPN TOOLS`；新版已自动启动，
  进程 PID 31680、`Responding=True`，进程名、exe 文件名与窗口标题均为 `CX VPN TOOLS`。

## 注意事项

- 历史 changelog 保留当时交付使用的旧名称，不回写历史记录。
