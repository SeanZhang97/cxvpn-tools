# 产品名称统一为 CXVPNTools

- 日期：2026-09-03
- 功能：产品命名统一
- 分支：`main`

## 需求概述

将项目当前产品名从旧的 `CX VPN TOOLS` / `CXVPN管理器` 统一为
`CXVPNTools`。本次不实施用户数据存储位置改造。

## 涉及文件

- `main.py`、`core/windows_desktop.py`、`core/proxy_guard.py`
- `build.py`、`build_protected.py`、`打包纯净版.bat`
- `ui/index.html`、`rule-packs/*.txt`
- `core/routing.py`、`core/routing_service.py`、`routing-service/`
- `tests/test_build_branding.py`、`tests/test_windows_desktop.py`、`tests/test_ui_polish.js`
- `AGENTS.md`、`design-qa.md`、`doc/repository/knowledge/`

## 关键设计

- 窗口标题、托盘名称、页面标题、侧栏品牌、exe 名、打包目录、spec 与纯净包名
  统一为 `CXVPNTools`。
- 开机自启注册表值改用 `CXVPNTools`；启动时如发现旧名启动项，保留其
  启用状态并平滑迁移到新 exe 路径。
- 当前单实例互斥改为 `Local\CXVPNTools.Singleton.v1`，同时持有旧互斥锁，
  并兼容唤醒旧窗口，避免更名过渡期新旧版本并行。
- 打包时优先保留 `dist/CXVPNTools` 的配置与规则包；首次更名打包可从
  `dist/CX VPN TOOLS` 或更旧的 `dist/CXVPN管理器` 恢复。
- 路由服务显示名和描述改为 `CXVPNTools`，服务版本升至 `0.6.1`，以触发
  已安装服务的可控更新。
- `%LOCALAPPDATA%\CXVPNManager` 和 `C:\ProgramData\CXVPNManager\RoutingService`
  继续作为旧版兼容数据路径；本次不迁移、删除或改写既有用户数据。

## 影响范围

- 影响桌面显示、打包产物命名、开机自启项、单实例与原生路由服务元数据。
- 不改变便携版 `config.json` / `webview_data` 的存储语义，不实施 AppData 迁移。

## 验证情况

- Python 语法检查：待最终复验。
- 品牌、Windows 桌面集成与 UI 静态回归：待最终复验。
- Rust 路由服务单元测试：6 项通过。
- 标准打包、配置保留与新版启动：待完成。

## 注意事项

- 源码中旧名仅能出现在明确命名为 `LEGACY_*` 的过渡兼容常量、路径说明与
  历史 changelog 中，不得再作为当前产品名展示。
