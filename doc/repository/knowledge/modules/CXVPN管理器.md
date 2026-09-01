# CX VPN TOOLS

模块: 业务工具 | 入口: `main.py`（pywebview 窗口）| 后端桥接: `api.py`
后台线程: `core/worker.py` | 连接编排: `core/vpn_service.py` | 构建: `build.py` | 界面: `ui/`
内置浏览器: `core/browser_win.py`（WebView2 原生窗口 + JS 桥接）
最后验证: 2026-09-01 | 分支: 非 Git 工作区

侧栏品牌区使用单行 `CX VPN` + `TOOLS` 标签组合，与 44px 图标垂直居中；
`TOOLS` 使用青色弱调标签，不再作为字距过宽的第二行副标题。

## 架构与调用链

- main.py 创建 pywebview 主窗口（ui/index.html，js_api=Api）并启动 Worker 线程;
  `webview.start(private_mode=False, storage_path=webview_data/)` 持久化 cookie。主窗口统一
  以 `hidden=True` 创建：普通启动由前端读取本地配置、完成首屏预展示并调用 `ui_ready`
  后显示，开机自启保持隐藏；普通启动 5 秒未收到通知时使用窗口显示兜底，防止前端异常
  导致窗口永久不可见。
- Api（api.py）暴露给 JS: 配置读写、VPN 列表/凭据、连接/断开、续期触发、
  浏览器（open_browser/get_browser/browser_reload/get_netlog）、
  人工验证码（get/submit_manual_captcha）、手动短信
  （get_sms_ui/submit_sms_code/dismiss_sms_ui）、VLM 测试、桌面设置
  （get_desktop_settings/set_startup_enabled）。
  统一域名分流通过 `get_routing_setup/get_routing_status/preview_routing/apply_routing`
  单独管理，普通 `save_config` 不得直接改写其启停状态；具体约束见
  `modules/Windows统一域名分流.md`。
  全局运行状态由 `core/ui_state_stream.py` 在后端聚合为带单调版本号的不可变快照；前端通过
  `get_ui_state_snapshot` 初始化、`wait_ui_state(version, 25)` 阻塞订阅变化。超时只作为
  心跳，不固定跨桥拉取四组接口；断线按 1～15 秒退避，重连后用完整快照恢复，不能只依赖
  可能丢失的增量事件。快照包含 worker/VPN、阻断弹窗、浏览器摘要、日志版本和脱敏代理遥测，
  不包含配置、凭据或 Mihomo Controller 地址/secret。命令完成后允许调用一次
  `refreshUiStateOnce` 提供即时反馈，但不得重新引入固定 `setInterval`；删除旧轮询函数时必须
  同步替换修复、授权和连接结束等延迟回调，避免运行时 `ReferenceError`。
  **worker 不暴露**: `worker._serializable = False` (见易错点)。
- Windows 桌面生命周期由 `core/windows_desktop.py` 管理。当前用户开机自启写入
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`，命令指向当前程序绝对
  路径并附带 `--startup`；该模式使用 pywebview `hidden=True` 静默启动。托盘基于
  已有 WinForms 消息循环创建 `NotifyIcon`，左键单击或菜单恢复窗口，菜单可显式退出；
  关闭到托盘不发送驻留气泡，人工验证码等需要介入的功能提醒仍会发送。入口使用
  `Local\CXVPNManager.Singleton.v1` 命名互斥保证单实例；重复启动只查找、恢复并置前
  标题为“CX VPN TOOLS”的既有窗口，不会创建第二个 Api、worker 或托盘图标。
  仅 `CloseReason.UserClosing` 且 `close_to_tray=true` 时拦截关闭；关机、注销和显式
  退出必须放行。`CloseReason.WindowsShutDown/TaskManagerClosing` 会先经
  `on_os_shutdown` 同步清扫本地系统代理残留（见 `modules/系统代理残留清扫.md`），
  `Api.start()` 首位做对应启动自检，修复结果以仅气泡的 `notify` 投递。
  退出会先解除人工等待、设置 worker `_stop_event` 并 `join`，该字段
  不得命名为 `_stop`，否则会覆盖 `threading.Thread._stop()`。`DesktopController`
  持有原生对象，必须保持 `_serializable=False`。
- **内置浏览器 (2026-08-27 起)**: 不再是无头 Playwright+截图流, 也不是
  独立窗口: `core/browser_win.NativeBrowser` 在主窗口 WinForms 表单内
  `Controls.Add` 第二个 WebView2 控件。主 UI 始终铺满窗口, 原生控件只覆盖
  浏览器页预留画布 (侧栏 216px, 工作区边距 22px, 顶栏 58px), 不再加宽
  窗口或横向分栏; 导航离开浏览器页时 `browser_set_visible(false)` 隐藏控件。
  同表面第二控件与主窗口共用 UserDataFolder 报 0x8007139F (独立窗口+同目录
  可行), 故面板用独立子目录 webview_data/embed/ (持久化, 首次需登录一次)。
  主窗口与面板的 profile 都落在软件目录的 `_internal/webview_data` 下
  (pywebview 在 private_mode=False 但给了 storage_path 时仍使用它), 因此
  两者都依赖软件所在目录可写、路径不过深。
  新窗口请求 (授权成功页) NewWindowRequested → 同控件 Navigate。
  自动化经 `PageShim`（CoreWebView2.ExecuteScriptAsync, 等待 Promise）驱动:
  `AddScriptToExecuteOnDocumentCreatedAsync` 注入 `window.__cx` (每次导航
  自动生效): 选择器引擎 (text=/tag:has-text/CSS 取最短文本可见匹配、
  同长度取最内层<=文档序靠后——外层容器与按钮同文时点容器冒泡不到
  handler; vis 排除 0×0 空矩形——站点 SDK 预插隐藏 #eject)、元素
  注册表句柄 (cx.q 返回 id, **0 为合法值**, 判空用 is not None)、
  fill (原生 value setter + input 事件)、坐标点击 (dispatch
  MouseEvent 序列) 与红圈编号点击标记（自动化可视化）。
  页面刷新不能通过 `eval_js(() => location.reload())` 等待结果：导航会销毁旧文档
  和页内结果槽。`PageShim.reload` 必须走 `CoreWebView2.Reload()`，以
  `NavigationCompleted → _loaded` 判定完成；`eval_js` 在空结果轮询分支也必须
  检查总截止时间，防止新文档持续返回 `undefined` 时无限循环。
- 抓包: WebResourceRequested/ResponseReceived (metadata) + 页内 fetch/XHR
  钩子 (完整采集文本、JSON、XML 和表单请求/响应体，不采集二进制响应体);
  worker 1s 排空 → 内存 500 条
  (UI 抓包面板) + 全量 `netlog.jsonl` (2MB 轮转) + xhr 记录进运行日志。
  页内 fetch/XHR 在记录时先以当前 `location.href` 将相对路径解析为绝对
  URL，因此面板和运行日志均保留协议、域名、路径与 query 参数。
  运行日志保留请求体/响应体原始文本和空白，不对 URL 或已采集内容
  进行二次截断。
- 浏览器可视层: `OVERLAY_JS` 通过 document-created 注入, Shadow DOM 隔离
  站点样式。浮动进度卡由 worker 推送真实五阶段状态, 点击标题收起/展开;
  “网络请求 N”切换底部抽屉, 显示最近请求并可展开请求体/响应体。
  请求详情的展开状态与详情自身滚动位置在 worker 每秒重推数据时按请求标识保留；
  请求快照未变化时不重建 DOM，新增请求时以当前可见请求作为外层滚动锚点，避免阅读
  请求体/响应体时被实时刷新拉回顶部。抽屉默认/最低
  高度为 186px，可通过顶部拖拽柄调整，上限为当前视口高度的 70%，左侧进度卡
  会跟随抽屉高度上移。手动打开但
  未执行续期时为 idle, 不显示伪进度。WebView2 的 document-created 注册是
  异步操作, 首次导航不能只依赖注册结果: `NavigationCompleted` 会对当前文档
  补注入并恢复最近 payload; `update_overlay` 检测 `window.__cxPanel` 缺失时
  立即重建; worker 每秒重推一次作为页面 DOM 重建后的自愈心跳。
  Shadow DOM 不继承主界面 `ui/style.css`，网络列表 `.net-table` 与请求详情
  `.net-detail` 的滚动条主题必须写在 `OVERLAY_JS` 内：8px 深色圆角滑块、透明
  轨道、hover 高亮并隐藏 WebKit 上下箭头。视口宽度不超过 1100px 时进度卡默认
  收起，避免遮挡远端登录表单；用户主动切换后不再自动改写状态，窄窗口打开请求
  抽屉时仍会收起进度卡。
- 原生 WebView2 是 WinForms 原生控件，层级高于 pywebview HTML 内容；HTML
  `.modal` 即使具有更高 CSS `z-index` 也不能覆盖它。验证码 `#capmodal`、短信
  `#smsmodal` 显示前必须先调用 `browser_set_visible(false)`；弹窗隐藏后仅当当前
  仍在浏览器页且两个阻断弹窗都已隐藏时，才能恢复原生浏览器，避免出现可见遮罩
  拦截主界面、弹窗卡片却被 WebView2 盖住的假死状态。
- **续期流程接口化 (2026-08-27 晚, 全量批处理)**: web_flow 除图形验证码
  (页面模拟点击) 外改调同源接口 (页内 fetch): 登录=填号+点获取验证码
  (DOM 触发验证码) → 等短信/登录 → 得 VPN 列表; 授权/续期**与工具配置
  的目标 VPN 无关 —— 页面有哪些行就批量处理哪些行**: 未授权/未激活行 →
  POST /vpn/batchLoginVpn 授权, 其余行 → POST /vpn/batchReLoginVpn/addTime
  续期 (均 phoneNumber&vpnIdsSel=**逗号拼接多 id**, 与站点 vpnLogin.js
  batchInput.join(",") 同款参数)。接口失败回退页面**逐行点击**
  (行 id 去重防页面重渲染后重复点击; 每行一次, 最多 30 行), 成功以面板内
  导航 VPNLoginSuccess 判定。
- Worker 循环 0.2s tick: 按需建/重建浏览器窗口 → 抓包排空 (1s) → 续期流程 →
  30s 断线重连（含 691 触发续期自愈、退避）。周期连接检查与实际拨号通过
  `_queue_auto_connect` 移交独立守护线程；主循环对 VPN 操作锁只做非阻塞尝试，
  因此拨号期间仍能领取手动打开浏览器、授权和 RAS 事件任务。窗口被用户关闭后按需重建。
  手动与自动续期由 `_renew_lock` 原子领取，`renewing` / `renew_queued` 暴露给
  总览页；`cancel_renew` 可取消排队任务，或通过 `renew_cancel_requested` 对运行中
  流程协作式中断。`web_flow` 在导航、验证码、短信等待、授权请求与页面兜底
  之间检查中断，人工验证码等待会由 API 同步唤醒。中断与失败只设置独立的
  `_renew_retry_after`（10 分钟），不再改写真实 `last_renew`。
- 连接链: api/worker → vpn_service.connect(name, creds) →
  有工具凭据时由 `ras_cred.set` 同步普通 RAS 凭据，再由 `eap_connect.prepare`
  为 EAP-MSCHAPv2 写入 `RasSetEapUserData` 并通过
  `RasGetEapUserIdentity(RASEAPF_NonInteractive)` 校验无界面身份。普通 PPTP 等条目
  直接把软件保存的真实账号密码交给异步 `RasDialW`；`RasDialFunc1` 回调记录状态，
  90 秒未完成时用本次 `HRASCONN` 调用 `RasHangUpW` 强制结束；返回 703 时把非交互身份的
  `RASEAPINFO` 交给 `RasDialW` 完成 EAP 连接。连接路径不得调用 `RasDialDlgW`，
  也不得复制 `RasGetCredentialsW` 返回的星号密码句柄；前者必然引入系统对话框，
  后者在当前 Win11 环境会把拨号用户提交成 `*\\`。`rasphone.pbk` 的静默开关只作为
  经典预览和进度窗口的兼容处理，并保持中文条目所在文件的原编码。
  **auto_connect 开关默认关**: `config.vpn_name` 保留为兼容字段，但产品语义是
  “默认 VPN”，只决定总览快捷入口和后台自动重连目标；手动连接通过
  `connect_vpn(name, mode)` 操作任意 VPN。已有其它连接时，首次调用只返回
  `needs_mode_choice`，UI 必须让用户明确选择 `switch`（逐条断开现有连接后再拨号）
  或 `parallel`（保留现有连接）。`disconnect_vpn(name)` 只断指定连接，
  `disconnect_all()` 才断开全部。首次接管 Windows 已有 VPN 时，软件无法读取系统保存
  密码的明文；直接连接会在拨号前返回 `needs_credentials` 并打开凭据弹框。若弹框由
  连接动作触发，保存成功后 UI 自动续接原目标与 `switch/parallel` 模式；主动维护凭据
  或新建 VPN 时只保存，不自动拨号。
  软件内新建 VPN 可同时填写 L2TP 预共享密钥和登录凭据；创建或保存凭据时立即同步
  工具、普通 RAS 与适用的 EAP 用户数据，但不拨号、不打开授权页。远端是否已授权
  不再作为保存前校验，也不维护待授权验证队列；实际连接返回 691 时再进入既有续期
  自愈流程。
  手动连接
  691 → renew_requested 续期自愈。部分 EAP-MSCHAPv2 失败会由 `RasDialW` 表层返回
  628；连接层按本次拨号时间与用户名匹配 EAP 事件 101，若 `int1=691` 则提升为认证
  失败并进入同一恢复链。756 仅表示前次拨号状态未释放，30 秒后重试且不触发授权。
  `RasDialW` 失败、超时或取消但返回非空 `HRASCONN` 时，连接层必须 `RasHangUpW` 并
  等待 3 秒完成状态机清理，避免授权后重连持续 756。错误 1460 归为可退避的网络超时。
  后台连接通过 `state.connection_action` 暴露
  `queued/connecting/connected/failed` 及 `automatic/authorization` 来源；总览在动作
  期间显示自动连接或授权后重连状态并禁用重复连接。授权成功且自动连接开启时显式调用
  `_reconnect_after_authorization` 立即重连默认 VPN，不依赖 30 秒周期检查。
  worker 对其它临时网络错误按 5、15、30、60 秒退避，连续失败 5 次后暂停；
  对系统无密码、配置/证书、电话簿和本机服务错误直接暂停自动重试；修改
  配置、更新凭据、续期或修复成功后通过 reset_backoff 解除暂停。自动连接开启时
  691 触发续期，冷却 10 分钟。自动连接只在当前没有任何 VPN 在线时补连默认 VPN；
  用户切换到其它 VPN 后，只要该连接仍在线就不得把默认 VPN 自动拉回。API 手动连接/断开
  与 Worker 自动拨号共用 VPN 操作锁。Worker 使用 `RasConnectionNotificationW` 监听全部
  RAS 建连/断开事件，事件合并 1 秒后刷新状态，并保留 30 秒轮询兜底；会话内维护
  `desired_vpn`，软件切换连接时先更新目标，旧连接断开不得被自动拉回。软件内主动断开会
  暂停本会话自动连接，直到用户再次手动连接；外部断开与网络/服务端踢线无法由 RAS 可靠
  区分，均只对当前期望连接应用退避重连。10 分钟内连续断开 5 次进入 flapping 熔断；
  普通断开事件不得触发网页授权，只有拨号明确返回 691 才进入授权恢复链。
  `RasDialW` 返回成功但 Windows 尚处于“正在连接”时，Worker 必须进入 30 秒确认窗口并
  保持 `connection_action.status=verifying`；手动连接、自动连接和授权后重连共用该确认门控，
  后端 API 也必须拒绝重复提交。Windows 已处于 `Connecting` 或 `Disconnecting` 过渡态时
  不得再次拨号；保存无关配置、重新开启自动连接和授权完成均不得清除在途确认。窗口到期仍
  未观察到 `Connected` 才计为一次失败并进入统一退避，成功路径不得调用会把 `_last_conn`
  清零的 `reset_backoff()`，避免形成约 3 秒一次的拨号风暴。确认超时检查、RAS 状态刷新和
  手动/自动拨号共用 VPN 操作锁，防止临界时刻并发重入；RAS 事件合并以首个事件确定 1 秒
  刷新期限，后续事件不得持续延后刷新。若用户在确认期内明确连接另一条 VPN，新目标必须
  立即覆盖旧 `connection_action`；新连接立即成功、失败、切换断开失败或目标本就在线时都
  必须写入对应终态，禁止只清除确认截止时间而让旧 `verifying` 永久残留。
  worker 通过 `refresh_connections()` 更新
  `connections[]`、默认 VPN 的兼容 `connected` 状态、软件首次观察到的连接起始时间和
  `route_conflicts[]`；UI 通过后端版本化状态流接收该内存快照，不再每 2 秒跨桥轮询。
  活动连接时长优先由
  `RasEnumConnectionsW` 的连接句柄调用 `RasGetConnectionStatistics`，读取 Windows 维护的
  `dwConnectDuration` 并反推 `connected_at`，因此软件重启不会重置仍存续的 RAS 会话时长；
  `RASCONN` / `RAS_STATS` 必须按 `ras.h` 使用 `_pack_=4`，否则 64 位结构尺寸错误会返回
  632。系统 API 失败时才回退到软件首次观察时间，`connection_time_source` 标记为
  `system` 或 `observed`。系统 VPN 查询会按接口别名补充
  IPv4/IPv6 地址、前缀和路由；同时出现多个 `0.0.0.0/0`、多个 `::/0` 或不同 VPN
  的同协议族路由网段重叠时生成提示。刚建立连接时 `Get-VpnConnection` 可能短暂滞后，
  Worker 系统刷新需将其
  与 `rasdial` 实时会话合并，连接/断开操作完成后 UI 必须强制刷新；RAS 快路径必须
  按名称 `casefold` 后精确匹配，禁止子串匹配相似名称。
  Worker 同时缓存最近一次完整 VPN 配置快照及检查时间；`Api.list_vpns(false)` 复用该
  快照，只有明确的 `force_refresh=true` 才重新查询系统。主界面读取配置后会后台预取
  VPN 列表，进入配置页复用同一个 Promise/前端缓存；普通预取不能吞掉后续强制刷新。
- 续期链: web_flow.ensure_authorized = 填手机号 → 先启动
  sms_receiver.Catcher（后台轮询通知库暂存验证码）→ 等站点
  window.captchaIns 就绪 (≤15s, 冷启动 layui/验证码 SDK 异步加载,
  过早点击按钮绑定未完成/实例未就绪=静默空操作) → 点获取验证码
  以"弹窗出现"为准重试 3 次 → 触发验证码
  （captcha_handler 混合方案/VLM, _wait_result 以"站点已发短信"为权威通过
  信号——通过后弹窗可能切到短信步骤而仍在, 弹窗仍在≠失败; 图存
  captcha_cache/ 保留 10 张，AI 回复/思考记日志）→ 等码 0.5s 轮询
  catcher.poll() 与 api.poll_manual_sms() 并行 (检测到 _list_ready=
  用户已手动登录, 跳过; 登录本身走页面点击事件填码+点登录按钮,
  **不再直调 checkCode API** —— API 不触发页面跳转/会话建立) →
  等跳转 VPN 列表 (轮询 15s) → 全量批处理 (见上条)。站点接口另含
  下线 POST /vpn/closeUser/user (vpnIds&phoneNumber)、IP 切换
  /vpn/batchReLoginVpn/changeIp、单行 /vpn/loginVpn (reLogin 参数)。
  `/vpn/batchLoginVpn` 与 `/vpn/batchReLoginVpn/addTime` 成功响应中的
  `data.expTime` 是授权到期时间的权威来源；Worker 持久化每条 VPN 到期时间、最早到期时间、
  成功时间和触发来源。自动续期取“上次成功时间 + renew_hours”与“最早到期前 15 分钟”的
  较早值，进程重启不能重置计时。接口未返回到期时间时才按“成功时间 + 8 小时”和次日
  02:00 的较早值兜底。没有任何持久记录表示外部授权状态未知，启动时不盲目续期；普通
  断线先重连，只有 691 才授权并建立新的到期基准。现有续期请求锁保证手动、定时和 691
  恢复来源同一时间最多只有一个排队或运行任务。
- 图标验证码的图片下载默认遵循 Windows 系统代理；VPN 或代理工具退出后若残留的本地
  代理端口拒绝连接，`captcha_handler` 会禁用系统代理并按真实路由直连重试一次。
  视觉模型请求的 HTTP、连接、超时和底层网络错误统一标记为服务不可用；
  `handle_captcha()` 必须立即停止自动识别并进入工具内人工点选，不能继续消耗自动尝试次数，
  也不能让该类异常冒泡到 Worker 后触发整次授权的 10 分钟冷却。

## 配置与便携

- config.json 与 webview_data/（cookie 持久化）位于 exe 同级
  （core/config.py 以 sys.frozen 定 BASE）；
  字段: phone、vpn_name（默认 VPN）、**creds（持久凭据，回显用）**、
  credential_status（仅保留本机写入异常 `windows_sync_failed`；启动时迁移清理旧的
  `pending_authorization` / `validated` / `validation_failed`）、renew_hours、auto_renew、
  authorization{last_success_at,expires_at,source,vpn_expiries}、auto_connect、close_to_tray、
  captcha_max_attempts、sms_timeout、
  sms{method,email{provider,host,port,username,password,mailbox,subject,sender,poll_interval}}、
  vlm{base,key,model}、routing{enabled,traffic_mode,physical_interface,proxy_strategy,
  proxy_providers[],default_outbound,dns_servers,controller_port,controller_secret,rules}。
  `proxy_providers[]` 保存多个订阅的 ID、名称、URL、启用状态、节点策略、更新周期和
  节点筛选；旧 `proxy_provider_url` 在分流配置规范化时自动迁移。
  `renew_hours` 默认 7 小时，可在 1～8 小时范围内修改。开机自启
  不写入 config，以 Windows 注册表当前值为准。
- Windows 当前用户 VPN 的 IPv4/IPv6“远程网络默认网关”分别存于用户
  `rasphone.pbk` 的 `IpPrioritizeRemote` / `Ipv6PrioritizeRemote`。VPN 列表读取这两个
  字段，缺失时用 `Get-VpnConnection.SplitTunneling` 反向推导；新增和编辑弹框可独立
  修改。写入保持电话簿 BOM、原编码和换行格式，并使用同目录临时文件原子替换；修改
  已连接条目后需重新连接才会生成新的系统路由。
- 删除系统 VPN 成功后，`Api.remove_vpn` 必须同步清理同名 `vpn_name` 目标引用、
  `creds` 凭据副本、`credential_status` 和 Worker 待验证队列并持久化；系统删除失败
  时不得修改工具配置，避免用户无法重试。
- build.py: PyInstaller onedir --noconsole --icon；collect-all clr_loader、
  collect-submodules webview（2026-08-27 起不再 collect playwright）；
  打包 `runtime/routing` 中锁定版本的 Mihomo/WinSW 与许可证；重打包前自动备份并恢复
  dist 下 config.json。
- 分发: 整个 dist/CXVPN管理器 文件夹 zip；目标机需 Win10/11 + Edge + WebView2
  + 已配对 Phone Link。
- 图标: make_icon.py 生成黑底圆角流星 icon.ico 与 ui/logo.png；
  改图标后需删除 build_tmp 强制重嵌 EXE 图标（PyInstaller 缓存不感知 icon 变化）。
- 源码在 C:\develop\workspace\cxvpn-manager；`main.py`、`api.py`、`core/`、`ui/`
  或打包资源发生变化后，按项目规则执行
  `uv run --with pyinstaller python build.py`，并核对 dist 用户配置未丢失。

## 页面

侧栏新增“网络代理”日常入口，位于“VPN 配置”和“域名分流”之间。该页把订阅选择、节点选择、
规则/全局模式与一键启停收敛到一个工作台；“查看全部节点”进入同页二级节点列表，复用
`ui/routing.js` 的代理组、测速和选点能力。高级域名/VPN 出口规则仍由“域名分流”负责，
避免简单翻墙场景必须理解完整分流配置。
代理服务、启停和遥测连接状态由后端状态流异步更新，相关低频状态区必须使用
`aria-live="polite"`/`role="status"` 播报；实时上下行速率不得放入 live region，避免高频
WebSocket 帧持续打断屏幕阅读器。
代理开启属于可能改变 Windows 路由的事务，失败时必须同时保留短时 toast 和代理首页内的持久
错误反馈；标题需要明确“代理未开启，系统路由已恢复”，正文展示后端已脱敏的具体原因，并允许
用户手动关闭。不能只依赖约数秒后消失的 toast，否则节点实时检测或服务启动失败后用户无法复查。

原“AI 模型”导航已调整为“自动化配置”，页面上半部分选择验证码接收来源：Windows
手机连接（通知同步）或邮箱转发（IMAP SSL）；邮箱配置支持常用服务商
预设、固定主题、可选发件人和只读连通性测试。页面下半部分保留原 OpenAI 兼容视觉模型
配置。帮助说明分别给出 Phone Link、iPhone 快捷指令和 Android SmsForwarder 的完整配置步骤。
帮助折叠项的标题、类型标签和展开符号使用固定三列网格，类型标签统一在 88px 中列居中，
避免标题长度变化导致标签错位。浏览器页命令栏除打开、刷新外，还提供「执行自动化」入口；
它与总览「立即处理授权」共用同一 `renew_now` / `cancel_renew` 状态和按钮处理函数，任务运行时
两个入口同步显示中断动作，浏览器尚未启动时由 Worker 按既有流程自动创建并执行。

总览采用状态带式工作台：顶部以青色波形集中显示后台网络状态、当前连接容量和授权有效期；
中部左侧突出默认 VPN、最近连接、协议/服务器和连接主操作，并在同一行展示「网络出口」卡片；
卡片分别显示默认 IPv4 路由对应的本机物理网卡地址、国内探测点看到的公网出口和海外探测点
看到的国外出口，支持逐项复制与手动刷新。`Api.get_ip_info()` 使用五分钟缓存和守护线程刷新，
连接路由变化后前端触发强制刷新；刷新进行中再次发生路由变化时必须排队补做一次强制刷新。
国内探测始终绕过 Windows 系统代理并跟随默认 IPv4 路由：超星 VPN 开启远程默认网关并连接时
应反映其北京公网出口，关闭默认网关或断开后应恢复本地成都运营商出口；该结果与国外代理节点
出口相互独立。Cloudflare `/cdn-cgi/trace` 海外探测需通用识别活跃的
WireGuard、Wintun、TUN、TAP、VPN 等虚拟隧道：存在活跃隧道且系统代理可用时走代理，以覆盖
代理承载的 VPN 节点出口；没有活跃隧道时绕过断开后残留的本地代理。判定不得依赖具体工具
或进程名称，且应排除 WAN Miniport、Hyper-V、VMware、VirtualBox、Docker、WSL 等非目标虚拟
适配器。海外探测以 `cloudflare.com` 为主、`www.cloudflare.com` 为备用，取得权威 IP 后才用带明确 IP 参数的 `ipapi.co` 补充位置；位置
查询失败不得清空 IP。任一探测点失败时必须独立降级，已有成功数据保留并在前端明确标记为
「旧数据」，不得显示为刚刚更新，也不得阻塞或中断 VPN 状态轮询。网络出口卡片与活动连接区共享水平内边距，右边框必须
与连接行对齐；卡片到自动化区分隔线需保持紧凑，公网和国外 IP 在 1080px 最小窗口宽度下
仍须单行完整显示，辅助位置和运营商可省略并通过 `title` 查看完整值；卡片不展示国内外出口
差异结论。默认 VPN 下方保留至少五个连接槽位，右侧放
自动续期、自动连接、开机自启和关闭到托盘四项自动化设置，并在关闭到托盘下方以同一布局体系放置「修复 VPN 服务」维护入口；底部授权区展示真实五阶段进度和
「立即处理授权」，运行时授权按钮切换为「中断授权流程」。
没有默认 VPN 时主操作显示「配置 VPN」并跳转配置页；有默认项时按连接状态只显示连接或
断开主操作，主操作旁不再提供重复跳转配置页的省略号。真实 VPN 配置优先占用五槽，剩余
槽位显示「添加 VPN 配置」入口；占位槽不含
虚构名称或连接状态。空槽位在总览直接打开新增 VPN 弹框，真实行右侧省略号直接打开对应 VPN 编辑弹框；
保存后强制重读 Windows VPN 快照，就地更新 VPN 配置表、总览连接区及统计，不切换到配置页。真实行显示状态环、默认标记、连接状态和逐条连接/断开入口，两条以上
在线时显示「断开全部」；默认路由或网段冲突在列表上方持续提示。
未连接行的状态文字与右侧操作区保留独立间距；圆形连接/断开按钮进入忙碌态时必须保留圆形
结构并显示加载环，禁止以 `textContent` 写入“连接中/断开中”造成文字换行；已连接状态使用
细线电源图标表达断开，不使用易与录制停止混淆的方块。授权流程使用
五个编号节点和四段贯穿线：已完成节点显示勾选和“通过”，当前节点显示青色内环，后续节点
显示“待执行”；连接线进度按 `max(0, step - 1)` 映射到 0～4，状态数据仍来自
`browser_progress`，不得为贴合概念稿伪造已完成阶段。
侧栏 9 个导航项、自动化设置、连接操作和维修入口使用统一 24 × 24 线性 SVG 图标；其中
「自动化配置」使用魔法棒与星芒图形，区别于普通设置类滑杆图标。按钮
文字仍作为可访问名称，并维护 `aria-current` 与键盘聚焦状态。导航焦点环只在 Tab 键盘导航模式中显示，避免 WebView2 启动初始焦点与 active 边框叠加。高度不超过 920px 时总览
压缩授权条，并将主区底部内边距收紧为 20px；高度不超过 820px 时还需压缩标题、状态带、网络出口、连接行、自动化区和授权条，
并将主区底部内边距收紧为 8px；高度不超过 700px 时底部内边距进一步收紧为 4px。1440 × 900 默认窗口、高 DPI 等效视口及
1080 × 680 最小窗口均应保证总览首屏无竖向滚动条；维修入口不再额外占用底部高度。
主内容 `main` 作为 flex 子项必须保持 `min-width: 0; overflow-x: hidden`，否则 Windows
WebView2 的轻微横向溢出会生成横向滚动条并继续诱发竖向滚动；需要横向滚动的表格由内部
`.table-card` 独立承载。
开关保存失败会恢复原值。
连接失败后总览显示 `state.connection_error.message`；633、692、711 等本机服务类
  错误会把服务状态改为“建议修复 VPN 服务”。执行前必须明确提示会断开全部 VPN 并
  重启系统服务，用户确认后才可启动修复。修复流程会断开全部 RAS 连接并重启
PolicyAgent、IKEEXT 和 RasMan；非管理员调用必须显示 UAC，但授权后的 PowerShell 进程
使用隐藏窗口。RasMan 无法正常停止时，只有确认宿主为 `System32\\svchost.exe` 且该进程
仅托管 RasMan 单一服务，才允许终止旧进程；共享 svchost 绝不能结束。服务进入 `Stopped`
或 SCM 以新 PID 拉起均表示旧 RAS 状态已清除，最后必须再次确认三个核心服务为 Running。
状态由 `get_state` 的 `browser_progress`、`renewing`、`renew_queued`、
`renew_cancel_pending` 驱动。
远端 VPN 列表授权完成后会就地更新行状态；完成提示使用不参与页面排版的顶部瞬时浮层，
数秒后自动移除，禁止向列表容器插入会挤压表格内容的常驻提示条。
VPN 配置（系统 VPN 表格 + 默认网关摘要 + 行内连接/断开、设为默认、凭据/编辑/删除；
编辑弹框可独立控制 IPv4、IPv6 远程默认网关；连接第二条时
弹出“切换连接/同时连接”选择；
选择“切换连接”时，断开当前连接与拨入目标连接处于同一个手动操作互斥区；目标连接在线后，
自动连接逻辑必须避让该活动连接，不得重新拉起默认 VPN；
新增弹窗可直接创建 Windows VPN，并按类型配置 L2TP 预共享密钥及可选登录凭据；
总览和配置表只在本机凭据同步失败时显示“Windows 同步失败”，不展示待授权验证状态；
应用启动时后台预取系统 VPN，首次进入配置页复用缓存；连接/断开结束后直接使用 Worker
已刷新的 `profiles` 快照，并按 VPN 名称和渲染键原位更新变化行，禁止先清空表格显示加载
占位；Worker 的 `vpn_status.profiles`、连接完成快照和 `list_vpns` 结果必须统一写入前端全局
`VPN_LIST`，不得因「VPN 配置」页当前不可见而跳过状态同步；隐藏页只更新快照，当前页或切入
配置页时从最新快照渲染。手动刷新和配置变更使用强制刷新，已有列表刷新失败时保留当前内容；
总览首屏在 Windows 查询完成前，使用 `config.json` 的 `vpn_name` 与 `creds` 键名生成
仅含名称的临时行，并明确显示“正在核对”；`config.json` 不含服务器、协议、路由和实时
连接状态，相关字段不得在预展示阶段伪造。Worker 快照或 `list_vpns` 返回后必须以 Windows
真实结果替换临时行，包括真实结果为空的情况；
VPN 表格操作区使用固定五槽栅格（连接、默认状态、凭据、编辑、删除），默认行以
“已默认”只读状态占位，保证所有行按钮纵向对齐；
文本、密码、数字、选择框、文本域及自定义下拉的聚焦态统一为 1px 青色边框，不叠加
box-shadow 或外扩 outline；checkbox/switch 与普通按钮继续使用各自的可访问焦点态；
邮箱授权码、AI API Key、VPN 凭据密码、L2TP 预共享密钥和新增 VPN 登录密码共用 `.eye` 眼睛开闭控件：密文时显示睁眼、明文时显示闭眼，并同步 `aria-label` / `aria-pressed` / `title`；VPN 弹框重新打开时必须恢复密文。
自定义深色下拉；删除前使用应用内
确认弹窗；凭据弹窗回显已存用户名与密码、眼睛明文、留空提交=保持不变）、
超星账号（手机号 + 1–8 小时续期间隔校验 + 行内保存反馈）、AI 模型
（base/key/模型完整性与 URL 校验 + 眼睛明文；测试前先保存当前表单，避免测试旧配置）、浏览器
（顶部命令栏 + 原生 WebView2 单一画布；执行进度卡可从标题收起/展开；
“网络请求 N”在原生页面内打开底部请求抽屉并可查看请求/响应体详情；
打开浏览器/刷新按钮）、
短信验证码弹窗 #smsmodal（手动输入，与自动捕获并行谁先到用谁；可「关闭」,
关闭仅隐藏 UI 不影响后台捕获, 新流程 sms_show 时按新 id 重弹）、
帮助说明（四步快速开始 + 五组折叠章节，含 Phone Link 配对、凭据和授权机制）、
运行日志（实时标记、记录计数、空状态与复制入口）。操作结果统一由四级轻提示显示：
success / error / warning / info 分别具有独立标题、颜色和播报角色；前置条件不足必须
使用 warning，不能伪装成成功。success 图标使用固定路径的线性 SVG 勾选，以圆头圆角笔画居中渲染，不使用受系统字体影响的 `✓` 字符。凭据、验证码、短信、VPN 编辑和危险确认弹框共用
统一标题、说明与底部操作区，危险确认默认聚焦取消按钮。
侧栏使用可键盘聚焦的语义化按钮并维护 `aria-current`；通用按钮、输入框和开关均有
`focus-visible` 状态。设置页限制最大内容宽度，表格允许横向滚动，弹窗按视口约束
宽高。UI 状态更新必须复用单一版本化订阅；日志正文仅在运行日志页且日志版本变化时读取，
浏览器摘要优先复用同一快照，避免无意义桥接调用。

## UI 强制规则: 弹框右上角关闭叉号

- **所有弹框右上角必须有关闭叉号** (项目强制规则, 2026-08-27 起)。
- 实现机制: app.js `injectModalClose()` 启动时为所有 `.modal-card` 自动
  prepend `.modal-x` 叉号, MutationObserver 保证未来动态新增弹框同样自动
  补上; document 级 click 委托 → `closeModal()` 隐藏弹框。新写弹框只需
  `.modal > .modal-card` 结构, **无需手写关闭按钮**, 从机制上杜绝"新弹框
  无法关闭"。
- 带后台状态的弹框在 `closeModal()` 内按 id 同步通知后端: #smsmodal →
  `dismiss_sms_ui()`; #capmodal → `dismiss_manual_captcha()` (解除 worker
  挂起, 本次按失败处理)。新弹框如有类似后台等待, 须在 closeModal 内补钩子。
- 样式: style.css `.modal-x` (绝对定位右上 14px); `.modal-card` 必须
  `position: relative`, h3 预留 `padding-right: 34px`。

## 易错点

- 通用 `.hidden { display:none !important }` 是弹窗/下拉隐藏的依赖，
  不可删除（仅 .modal.hidden 不覆盖 .dd-list）。
- pywebview: 主线程 start 后才能从非主线程 create_window; evaluate_js
  等待 Promise 但重脚本会阻塞调用线程; loaded 事件后注入的页内钩子
  (window.__cx) 每次整页导航都会丢失, 必须在 loaded 重注入。
- WebView2 `AddScriptToExecuteOnDocumentCreatedAsync` 返回异步任务, 若注册后立即
  `Navigate`, 第一次文档可能错过脚本；面板脚本必须同时具备导航完成补注入、
  最近状态缓存和周期自愈，不能把 `window.__cxPanel === false` 当成普通推送成功。
- CSS 层级无法让 pywebview 页面弹窗跨过同窗体的原生 WebView2 控件；新增任何
  可能在浏览器页异步出现的 HTML 阻断弹窗时，必须纳入原生浏览器显隐协调，并在
  多弹窗并存时以“全部阻断弹窗均隐藏”为恢复条件。
- 凭据机制见 common/Windows-VPN凭据与RAS-API.md：Windows 密码不能读取为明文，
  当前 Win11 实测的星号密码句柄会提交错误身份，因此软件没有凭据副本时必须先提示补录，
  不得直接拨号。用户补录成功后工具持久存一份并同步 Windows。
- config.json 含 API Key 与 VPN 密码明文，分发前清空。
- rasdial 非法参数会"USAGE + 退出码 0"假成功，判定必须查输出首行（vpn_connect._is_usage）。
- **js_api 属性遍历假死**: pywebview 每次导航完成都 `inject_pywebview`
  递归遍历 js_api 全部公开属性生成 JS 桥; 持有 Window/WinForms 原生对象
  的公开属性 (如 worker.main_window.native) 会陷 .NET 结构体自引用
  (`Bounds.Empty.Empty…`) 无限递归 + 反射开销 ≈20s 启动假死 (window.py
  `_api_call` 的 `event.wait(20)` 节奏)。此类属性必须标
  `_serializable = False` (Api.worker 已标)。

## 当前状态与待办（2026-08-27 晚；2026-08-31 补充）

- 已验证 (冒烟真机): 主窗口内嵌浏览器、document-created 钩子注入、
  抓包 (含响应体)、eval_js Promise、接口清单 (checkCode/addTime/getSmsCode/
  验证码 JSONP)。
- 已验证 (2026-08-31, 打包版 run.log 18:11): 内嵌面板登录态 + 完整自动续期
  一轮 (getSmsCode→checkCode→batchLoginVpn/batchReLoginVpn/addTime) 端到端。
- 已修复: 图标验证码"第一次已通过却判失败"; 新窗口请求甩系统浏览器
  (OPEN_EXTERNAL 默认 True 路径已绕过, 内嵌控件自行 Handled+Navigate)。
- 待端到端: 内嵌面板首次登录 (embed 目录新 cookie) + 接口续期一轮。
- 待补抓: 授权接口 (方便时下线→授权); 届时可把授权也接口化。

## 已知易错点（2026-08-31 修复）

- `RasDialW` 的 notifier 为空时是同步调用，网络黑洞或 PPP 协商停滞可让调用长时间
  不返回。连接路径必须保留非空 `RasDialFunc1`、90 秒硬超时和超时 `RasHangUpW`，
  不得退回同步拨号。
- 自动连接不得在 worker 主循环直接执行。`_queue_auto_connect` 的独立任务与
  `_try_vpn_operation` 的非阻塞锁探测是浏览器、续期和 RAS 事件保持可用的边界；
  新增长耗时外部调用时也必须遵循同样隔离原则。
- 日志证据链应同时出现 `[browser] 已提交...` 与 `[worker] 已领取...`，拨号应包含
  worker 来源、异步 RAS 开始、状态变化、结束结果和耗时。只有提交日志而没有领取日志
  表示 worker 未调度；有开始无终态且超过硬超时则表示超时取消链本身异常。
