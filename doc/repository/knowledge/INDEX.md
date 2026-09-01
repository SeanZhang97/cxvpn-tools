# 知识库索引 INDEX

最后验证: 2026-09-01 | 分支: 非 Git 工作区

## modules

| 模块 | 关键词 | 代码路径 | 知识文件 |
|---|---|---|---|
| CX VPN TOOLS | pywebview, WinForms, worker, vpn_service, 版本化状态流, wait_ui_state, 网络出口, IP, 续期, 开机自启, 系统托盘, 便携EXE, config | main.py, api.py, core/ui_state_stream.py, core/ip_info.py, core/, ui/, build.py | modules/CXVPN管理器.md |
| 系统代理残留清扫 | 系统代理残留, proxy_guard, ProxyEnable, 脏标记, 开机自检, 关机清扫, CloseReason, 本地代理端口无人监听 | core/proxy_guard.py, api.py, core/windows_desktop.py, main.py | modules/系统代理残留清扫.md |
| Windows 统一域名分流 | 网络代理, system-proxy, Proxy Guard, routing schema, 内置规则包, 节点测速, 手动选点, 节点筛选, 节点排序, 订阅流量, 套餐到期, WebSocket, 后端遥测中继, 实时流量, Mihomo, Named Pipe, Windows Service, TUN, DNS分层, proxy-provider, Windows VPN, Clash Verge Rev, 常驻核心, mixed-port, 订阅引导 | core/routing.py, core/mihomo_telemetry.py, core/routing_rules.py, core/routing_service.py, routing-service/, core/routing_speedtest.py, core/routing_selection.py, core/routing_updates.py, core/subscription_store.py, api.py, ui/proxy.js, ui/routing.js, ui/routing_nodes.js, ui/routing_telemetry.js, runtime/routing/, build_runtime.py | modules/Windows统一域名分流.md |

## common

| 主题 | 关键词 | 知识文件 |
|---|---|---|
| 短信验证码捕获 | wpndatabase.db, Phone Link, iPhone 快捷指令, IMAP, 邮箱授权码, toast XML, FILETIME | common/Windows短信验证码捕获.md |
| 超星图标点选验证码 | sprite, iconclick, VLM, 点选坐标, Referer | common/超星图标点选验证码识别.md |
| Windows VPN 凭据 | RasDialW, RasSetCredentials, RasSetEapUserData, RASEAPF_NonInteractive, EAP-MSCHAPv2, rasdial, 691, 703, Get-VpnConnection, 事件日志 | common/Windows-VPN凭据与RAS-API.md |
| 打包与分发 | 打包, 分发, 纯净版, zip, build.py, WebView2, 依赖环境, 凭据清理 | common/打包与分发.md |
| Git 仓库管理 | Git, GitHub, gitignore, 敏感配置, 构建产物, 运行时二进制 | common/Git仓库管理.md |
