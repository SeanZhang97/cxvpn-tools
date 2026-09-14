# Codex 代理配置同步

模块：Codex 本机代理配置同步  
关键词：`CODEX_HOME`、`config.toml`、`mixed_port`、手动启停、快照恢复、TOML 原子写入

代码路径：`core/codex_proxy.py`、`api.py`、`ui/index.html`、`ui/app.js`

最后验证：2026-09-14 | 分支：main

## 当前事实

- Codex 配置路径优先取环境变量 `CODEX_HOME/config.toml`，否则为 `%USERPROFILE%\\.codex\\config.toml`。插件缓存目录不参与读写。
- Codex 代理仅通过 Codex 页面按钮调用 `Api.sync_codex_proxy()` 手动开启，也可调用 `Api.restore_codex_proxy()` 手动关闭。`RoutingManager` 不依赖 Codex 配置模块；软件启动、已有代理运行状态校正、系统代理启用、TUN / 服务启动事务提交和端口变更均不写入 Codex 字段。
- 软件代理关闭保留联动：页面应用、首页和托盘等入口进入 `Api._apply_routing_locked()`；路由事务与应用配置保存成功，且状态由启用变为关闭后，调用 `_close_codex_after_proxy_disabled()` 清理 Codex 字段。配置保存失败回滚、代理关闭失败或原本已关闭时不触发清理。清理失败或用户字段冲突通过既有 warnings 返回，不撤销软件代理关闭；有字段变化时提示重启 Codex。
- 系统代理异常残留清扫仍由 `proxy_guard` 独立负责，启动修复不再连带移除 Codex 配置。已有 Codex 配置与快照在升级、启动和网络代理开启时保留，直到用户手动操作或成功关闭软件代理。
- Codex 页面提供手动入口（开启/关闭命名）：`api.sync_codex_proxy()` 开启 Codex 代理（从当前 routing 配置动态取 mixed_port）、`api.restore_codex_proxy()` 关闭 Codex 代理（幂等；快照有记录时仅删除仍等于最后写入值的字段，快照缺失时按写入模式兜底识别并删除 `http://127.0.0.1:<port>` 代理变量、默认 NO_PROXY 与 `respect_system_proxy=true`，用户改过的字段保留并警告）与 `api.read_codex_config()`（只读返回原文，限 512KB）。`get_codex_status()` 通过 `codex_proxy.status()` 返回快照字段数、上次同步端口、偏离字段与无快照残留字段列表，不返回配置正文。
- UI 查看弹窗直接展示原文（无遮蔽/切换控件，右上角叉号关闭）；日志不记录任何配置正文。
- 页面主按钮按配置文件状态动态切换：快照有托管记录或检测到无快照残留字段时显示「关闭 Codex 代理」（需确认，走 `restore_codex_proxy`），否则显示「开启 Codex 代理」（走 `sync_codex_proxy`）；操作完成后刷新状态并反转按钮。
- 页面进入、状态刷新及查看配置均只读。mixed_port 变更后保留原 Codex 端口并提示手动关闭后重新开启；开启时读取当时的规范化 routing.mixed_port，无新增公共配置字段。
- 同步只处理 `[features]`、`[mcp_servers.node_repl.env]` 和 `[shell_environment_policy.set]` 的托管键。行级文本合并保留其他 section、键、注释和 UTF-8 内容，候选文本使用 `tomllib` 校验后再原子替换。
- 首次托管字段的原始存在状态和值，以及最后写入值保存在 `%LOCALAPPDATA%\CXVPNTools\codex_proxy_snapshot.json`。关闭代理时只删除仍等于最后写入值的字段；用户手动修改的字段保留并记录警告。
- Codex 图标使用官方六片风叶形 mark（viewBox 0 0 100 100 的单 path fill 图形），经 `.codex-nav-icon` 与 `.codex-module-icon` 覆写项目默认 stroke 风格。

## 边界

该配置只影响读取 Codex 配置环境变量的进程。内置浏览器、插件独立进程和不继承 `config.toml` 的请求不受它控制；TUN 接管模式仍可同步给显式读取这些变量的 Codex 进程，但不能据此声称所有网络请求都被强制代理。配置变化不会自动结束 Codex，已运行进程需要重启。

## 离线验证

`test_codex_proxy` 覆盖缺失配置、局部更新、section 补齐、端口变更、幂等、关闭移除、手动修改保护、损坏 TOML、权限/写入中断、中文/UTF-8、read_config/status；`test_api_codex_proxy` 覆盖手动动态取端口、重启提示标记、查看日志脱敏、只读刷新/查看，以及关闭成功才清理、关闭失败/保存回滚不清理、清理失败不撤销软件代理关闭。

`test_proxycore` 在模拟 Windows 与原生服务的条件下断言底层路由操作不直接调用 Codex sync/restore，覆盖快切启停、启动残留修复、已有系统代理/TUN、完整事务及端口变更、完整停止。关闭联动由上层 API 提交成功后执行，原有 Windows 启用位补写、状态回读及用户会话刷新断言继续保留。
