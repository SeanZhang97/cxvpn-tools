# 应用名称统一为 CX VPN TOOLS

- 日期：2026-09-01
- 功能名：应用品牌名称统一
- 分支：非 Git 工作区

## 需求概述与问题根因

主窗口、系统托盘和页面标识同时使用“CX VPN 管理器”与“CX VPN MANAGER”，
品牌名称不一致。本次统一为“CX VPN TOOLS”。

## 涉及文件

- `main.py`
- `core/windows_desktop.py`
- `ui/index.html`
- `ui/style.css`
- `tests/test_ui_polish.js`
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
- 打包目录、exe 文件名、开机自启注册表键和单实例互斥名保持不变，避免破坏现有升级、配置保留与单实例兼容性。

## 影响范围

影响主窗口标题、托盘名称与菜单、HTML 标题及侧栏品牌字样；不改变程序包名、
运行目录、配置路径、注册表键、互斥名或业务逻辑。

## 验证情况

- `uv run python -m py_compile main.py core/windows_desktop.py`：通过。
- `uv run python -m unittest tests.test_windows_desktop`：12 个测试通过。
- `node --check ui/app.js` 与 `node tests/test_ui_polish.js`：通过，覆盖页面标题、
  侧栏单行品牌结构、`TOOLS` 标签样式、窗口标题常量与旧名称清理。
- 本地浏览器按 216px 侧栏实际渲染检查：品牌区无裁切，44px 图标与文字垂直居中，
  文字组合宽约 110px，`CX VPN` 与 `TOOLS` 同基线显示。
- `uv run --with pyinstaller python build.py`：通过，产物位于 `dist/CXVPN管理器/`，
  exe 时间戳为 2026-09-01 11:20:31。
- 打包前后 `config.json` 均为 2608 字节，SHA-256 均为
  `59930E02162091DCA3B740DE4B81AFEB7F6E1DE34A85D9A6D84D523A9D935060`，用户配置未丢失。
- 打包内 `_internal/ui/index.html` 已包含 `CX VPN TOOLS`；新版已自动启动，
  进程 PID 25424、`Responding=True`，实际窗口标题为 `CX VPN TOOLS`。

## 注意事项

- 历史 changelog 保留当时交付使用的旧名称，不回写历史记录。
