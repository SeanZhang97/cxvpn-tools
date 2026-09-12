# Codex 代理配置同步

模块：Codex 本机代理配置同步  
关键词：`CODEX_HOME`、`config.toml`、`mixed_port`、快照恢复、TOML 原子写入  
代码路径：`core/codex_proxy.py`、`core/routing.py`、`api.py`、`ui/index.html`、`ui/app.js`  
最后验证：2026-09-12 | 分支：main

## 当前事实

- Codex 配置路径优先取环境变量 `CODEX_HOME/config.toml`，否则为 `%USERPROFILE%\\.codex\\config.toml`。插件缓存目录不参与读写。
- `RoutingManager` 在完整系统代理事务提交后、系统代理快切启用回读成功后和启动对账确认代理有效后调用 `codex_proxy.sync()`；mixed-port 直接取规范化路由配置，未写死默认端口。
- 关闭系统接管成功后调用 `codex_proxy.restore()`。正常退出不会因为窗口关闭而恢复 Codex，因为现有程序允许系统代理继续保持；系统代理残留清扫仍由 `proxy_guard` 独立负责。
- 同步只处理 `[features]`、`[mcp_servers.node_repl.env]` 和 `[shell_environment_policy.set]` 的托管键。行级文本合并保留其他 section、键、注释和 UTF-8 内容，候选文本使用 `tomllib` 校验后再原子替换。
- 首次托管字段的原始存在状态和值，以及最后写入值保存在 `%LOCALAPPDATA%\\CXVPNTools\\codex_proxy_snapshot.json`。恢复只处理仍等于最后写入值的字段；用户手动修改的字段保留并记录警告。

## 边界

该配置只影响读取 Codex 配置环境变量的进程。内置浏览器、插件独立进程和不继承 `config.toml` 的请求不受它控制；TUN 接管模式仍可同步给显式读取这些变量的 Codex 进程，但不能据此声称所有网络请求都被强制代理。配置变化不会自动结束 Codex，已运行进程需要重启。

## 离线验证

`.venv\\Scripts\\python.exe -m unittest tests.test_codex_proxy` 覆盖缺失配置、局部更新、section 补齐、端口变更、幂等、恢复、手动修改保护、损坏 TOML、权限/写入中断和中文/UTF-8 内容。
