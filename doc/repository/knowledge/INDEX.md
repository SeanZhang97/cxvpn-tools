# 知识库索引 INDEX

最后验证: 2026-09-06 | 分支: main

## modules

| 模块 | 关键词 | 代码路径 | 知识文件 |
|---|---|---|---|
| 本地状态事务与恢复 | SQLite, WAL, 强杀恢复, 节点丢失, 批次, 配置版本, 兼容导出 | core/state_store.py, core/config.py, core/subscription_store.py, core/routing_tasks.py, core/routing_speedtest.py, api.py, ui/routing.js | modules/本地状态事务与恢复.md |
| 代理控制与运行状态 | 代理开关慢, 节点切换, 待机核心, fast_toggle_ready, 配置版本, 路由锁, IPC超时, 回读, 后台快照, 测速渲染, 订阅更新调度 | api.py, core/routing.py, core/routing_api_tasks.py, core/routing_tasks.py, core/routing_environment.py, core/routing_service.py, core/pipe_io.py, core/routing_speedtest.py, core/routing_updates.py, ui/proxy.js, ui/routing.js, routing-service/src/ | modules/代理控制与运行状态.md |
| CXVPNTools | pywebview, WinForms, worker, vpn_service, 版本化状态流, wait_ui_state, 网络出口, IP, 续期, 开机自启, 系统托盘, 便携EXE, config | main.py, api.py, core/ui_state_stream.py, core/ip_info.py, core/, ui/, build.py | modules/CXVPNTools.md |
| 系统代理残留清扫 | 系统代理残留, proxy_guard, ProxyEnable, 脏标记, 开机自检, 关机清扫, CloseReason, 本地代理端口无人监听 | core/proxy_guard.py, api.py, core/windows_desktop.py, main.py | modules/系统代理残留清扫.md |
| Codex 代理配置同步 | CODEX_HOME, config.toml, mixed_port, 快照恢复, TOML 原子写入 | core/codex_proxy.py, core/routing.py, api.py, ui/index.html, ui/app.js | modules/Codex代理配置同步.md |
| Windows 统一域名分流 | 网络代理, 快速开关, 系统代理快切, 订阅, 节点, 智能优选, 地区故障转移, 仅故障切换, 延迟容差百分比, 首选节点, 线路关键词, 规则, 本地规则包, rule-packs, 连接, 核心日志, system-proxy, Proxy Guard, routing schema, 节点测速, 手动选点, 节点筛选, 节点排序, 订阅流量, 套餐到期, WebSocket, 后端遥测中继, 版本化等待, 实时流量, Mihomo, Named Pipe, Windows Service, TUN, DNS分层, DNS高级模式, nameserver-policy, proxy-provider, Windows VPN, Clash Verge Rev, 常驻核心, mixed-port, 订阅引导, 系统代理绕过, 配置备份, 配置历史, 诊断包, 托盘快捷操作, 全局快捷键, 轻量模式 | core/routing.py, core/routing_auto_policy.py, core/config_maintenance.py, core/mihomo_telemetry.py, core/mihomo_activity.py, core/routing_rules.py, core/routing_service.py, core/ui_state_stream.py, core/windows_desktop.py, routing-service/, core/routing_speedtest.py, core/routing_selection.py, core/routing_updates.py, core/subscription_store.py, rule-packs/, api.py, main.py, ui/app.js, ui/proxy.js, ui/routing.js, ui/routing_workspace.js, ui/routing_activity.js, ui/routing_nodes.js, ui/routing_telemetry.js, runtime/routing/, build.py, build_protected.py, build_runtime.py | modules/Windows统一域名分流.md |

## common

| 主题 | 关键词 | 知识文件 |
|---|---|---|
| 短信验证码捕获 | wpndatabase.db, Phone Link, iPhone 快捷指令, IMAP, 邮箱授权码, toast XML, FILETIME | common/Windows短信验证码捕获.md |
| 超星图标点选验证码 | sprite, iconclick, VLM, 点选坐标, Referer | common/超星图标点选验证码识别.md |
| Windows VPN 凭据 | RasDialW, RasSetCredentials, RasSetEapUserData, RASEAPF_NonInteractive, EAP-MSCHAPv2, rasdial, 691, 703, Get-VpnConnection, 事件日志 | common/Windows-VPN凭据与RAS-API.md |
| 打包与分发 | 打包, 分发, 纯净版, zip, build.py, installer/, Inno Setup, 安装器, 离线依赖, WebView2, 依赖环境, 凭据清理 | common/打包与分发.md |
| Git 仓库管理 | Git, GitHub, gitignore, 敏感配置, 构建产物, 运行时二进制 | common/Git仓库管理.md |
