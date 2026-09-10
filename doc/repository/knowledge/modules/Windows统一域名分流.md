# Windows 统一域名分流

模块：Windows 网络路由 | 入口：`core/routing.py`、`core/routing_service.py`、`Api.apply_routing`
界面：`ui/proxy.js`、`ui/routing.js`、`ui/routing_workspace.js`、`ui/routing_activity.js`、`ui/routing_nodes.js`、`ui/routing_telemetry.js` | 原生服务：`routing-service/` | 运行时：`runtime/routing/` | 本地规则：`rule-packs/`
关键词：Mihomo, Named Pipe, Windows Service, system-proxy, 快速开关, 系统代理快切, Proxy Guard, TUN, routing schema, 本地规则包, rule-packs, proxy-provider, Windows VPN, 节点筛选, 节点排序, 订阅流量, 套餐到期, WebSocket, 连接日志, 核心日志, 后端遥测中继, 版本化状态流, 实时流量, Clash Verge Rev, 常驻核心, mixed-port, 订阅引导, 系统代理绕过, 配置备份, 配置历史, 诊断包, DNS高级模式, nameserver-policy, 托盘快捷操作, 全局快捷键, 轻量模式
最后验证：2026-09-10 | 分支：main

## 职责边界

代理启停、节点切换、待机配置一致性、路由互斥及 IPC 超时的当前边界，见
[代理控制与运行状态](代理控制与运行状态.md)。

- Python/pywebview 是业务控制面：保存规则、读取 Windows VPN/接口、验证前置条件、生成
  Mihomo 配置、执行普通权限预检，并通过受 ACL 保护的 Named Pipe 提交服务事务。
- Mihomo 是唯一数据面：系统代理模式监听回环 `mixed-port`，TUN 模式创建 `CXVPN-TUN`；
  两者互斥。Python 不转发数据包，也不自行实现代理协议。
- 第一方 Rust `CXVPNRoutingHost.exe` 以 LocalSystem 运行，负责配置二次校验、原子换版、
  超时回滚、当前用户系统代理快照/恢复、Mihomo 子进程监控、连续崩溃熔断和开机恢复。
  Windows Service 常驻，Mihomo 可独立启停；
  首次安装或服务二进制升级需要 UAC，之后开启、关闭、改规则和选点无需重复提权。
- `WinSW-x64.exe` 只保留为旧版迁移失败时的恢复材料，不再承担新版日常生命周期。
- 只支持域名粒度，不支持 URL 路径。路由层对 HTTPS 路径不可见，不能用静态路由表实现。

## 工作区 UI 组件约束

- 分流、订阅、节点、规则、连接和日志工作区的下拉框统一由
  `ui/routing.js::enhanceRoutingSelect` 渲染；其它工作区脚本通过
  `RoutingWorkspace.enhanceSelect` 复用同一个实现。原生 `<select>` 只保留为隐藏的值与
  无障碍语义载体，不能直接展示系统菜单。设置页已有的 `smart-select` 是另一套既有组件，
  不与分流工作区重复初始化。
- 静态下拉框清单由 `tests/test_ui_routing.js` 审计。新增 `<select>` 时必须同步接入对应自定义
  组件并登记清单，否则测试失败；动态创建的规则、订阅和节点下拉框必须在插入 DOM 后立即调用
  `enhanceRoutingSelect`。组件负责选中、禁用、浮层定位、滚动/缩放和键盘操作，不能只依赖 CSS
  改写原生控件外观。
- 所有工作区的滚动区域复用 `ui/style.css` 定义的全局深色滚动条变量和 WebView2 伪元素；该样式
  同时覆盖纵向、横向、轨道、滑块交互、滚动角和原生箭头按钮。主内容、侧栏、规则、连接、日志
  与下拉浮层使用稳定滚动条占位，局部 CSS 只允许调整尺寸，不能另设与全局冲突的颜色。
- 从主导航进入节点工作区时，节点分组默认按当前 `default_outbound` 指向的订阅定位；无明确指向时依次回退到有手动节点偏好的启用订阅和首个启用订阅。通过代理页或其他工作区明确打开某个订阅时，保留该调用方指定的分组。

## 配置与规则映射

`config.json.routing` 的稳定字段为：

- `enabled`：期望启用状态，默认 `false`。
- `schema_version`：当前为 `8`。旧配置缺少版本时按首版语义迁移为 `capture_mode=tun`、
  `builtin_rule_pack=off`，不得套用新安装默认值改变既有流量路径。
- `capture_mode`：`system-proxy` 或 `tun`，二者互斥。新安装推荐默认是
  `system-proxy`；TUN 是需要透明接管 UDP/不遵循系统代理应用时的高级模式。
- `traffic_mode`：`rule` 或 `global`。规则模式执行 `rules[]`；全局模式保留规则配置但生成
  运行配置时忽略域名规则，全部流量直接使用 `default_outbound`。切回规则模式后原规则恢复。
- `physical_interface`：物理直连绑定的 Windows 接口别名；留空时选择默认路由中优先级
  最高且非 VPN/TUN 的已连接接口。
- `proxy_strategy`：全部已启用订阅合并后的策略，支持 `url-test`、`fallback`、`select`。
  `aggregate_selection` 另保存“全部代理订阅”出口的 `auto`/`manual` 模式、所属订阅 ID 和
  原始节点名；手动模式只在该节点所属的已启用订阅 provider 上生成精确 `select` 组。
- `proxy_providers[]`：最多 16 个 Clash/Mihomo proxy-provider；字段为 `id`、`name`、
  `url`、`enabled`、`strategy`、`interval`、`filter`、`exclude_filter`、
  `download_route`、`download_proxy`、`user_agent`、`selection_mode`、`selected_node`、
  `auto_update`、`auto_policy`。`selection_mode=auto` 默认按 `strategy` 自动择优；
  `auto_policy.enabled=true` 时按有序地区和线路关键词生成分层故障转移组；`manual`
  固定使用 `selected_node`，并让已保存的智能策略休眠而不删除。`auto_update` 默认关闭。首版的 `proxy_provider_url` /
  `proxy_provider_interval` 会在规范化时迁移为 `default` 订阅；旧订阅缺少新增字段时默认
  使用 `auto` 下载出口和 `Clash-Verge` User-Agent。
- `default_outbound`：`physical`、`proxy`（全部订阅）、`proxy:<订阅 ID>`、`block` 或
  `vpn:<Windows VPN 名称>`。
- `builtin_rule_pack`：`off`、`local-direct-v1`、`cn-direct-v1`。可编辑规则内容来自
  `%LOCALAPPDATA%\CXVPNTools\rule-packs`；缺失时由包内只读默认文件补齐，保持离线且不后台下载；
  前者按 `TYPE,value[,no-resolve]` 保存本地域名与网段，后者每行保存一个国内域名后缀并自动叠加
  前者。文件使用 UTF-8，支持空行和整行 `#` 注释，用户可直接编辑；软件启动时一次性加载到
  内存，运行期间不监听、不重复读取；修改后重启软件加载，已启用代理还需重新应用一次配置。
  加载器限制大小和条目数，校验 IP、
  规则类型、IDNA 与选项，错误会带相对文件名和行号。`routing_rules.catalog()` 返回界面只读的
  结构化目录，每条
  包含顺序、类型、匹配值、出口和选项；展示结构不参与配置生成。
- `system_proxy_bypass`：包含不可关闭的 `lan=true`、国内规则包引用开关
  `include_cn_direct`、最多 100 个手动域名后缀 `domains[]` 和最多 100 个进程名 `processes[]`。
  Windows 系统代理入口始终用 `ProxyOverride` 绕过回环、私有网段、
  `.local` 和 `.lan`。系统代理模式下，每个自定义域名同时写成 `domain;*.domain`，使遵循
  Windows 系统代理的程序在入口处直接连接，不经过本机 `mixed-port`；TUN 模式下域名继续依靠
  最高优先级 `DOMAIN-SUFFIX,PHYSICAL` 规则。自定义进程始终生成 `PROCESS-NAME,PHYSICAL`，
  Windows `ProxyOverride` 不支持按进程绕过。进程规则要求 basename，拒绝路径和 Windows 非法
  文件名字符；运行配置固定 `find-process-mode=strict`。本地网段在 TUN 模式是否直连仍由内置
  规则包决定，不能把 Windows `ProxyOverride` 的作用误写成 TUN 规则。
- `include_cn_direct=true` 使用两层国内直连：先从软件启动时已加载的 `cn-direct-v1` 内存快照中
  提取国内域名后缀（不包含 `local-direct-v1` 的本地网段规则），与 `domains[]` 合并去重后写入
  Windows `ProxyOverride`，让已知域名不经过 `mixed-port`；未命中这些后缀但仍进入 Mihomo 的请求，
  在用户规则和本地规则包之后通过 `GEOIP,CN,PHYSICAL` 按目标 IP 兜底。编辑规则文件后需重启软件
  并重新应用。原生服务接受最多 2100 个合并后域名，对应最多 2000 个规则包域名和 100 个手动
  补充域名。
- `mixed_port`：系统代理模式的本机 mixed-port，默认 `17890`，不得与 Controller 重复。
- `dns_mode`：`simple` 或 `advanced`。新安装默认 `simple`，运行时使用阿里与腾讯 DNS 处理普通、
  引导、代理节点和 direct 出口解析，AliDNS 与 DNSPod DoH 作为加密 fallback，并用
  `fallback-filter.geoip-code=CN` 和污染地址段过滤选择结果；同时启用 `respect-rules` 与 fake-IP。
  简单模式不得直连 `1.1.1.1:443` 或 `8.8.8.8:443`，避免国内、校园和企业网络阻断国外 DoH
  时产生重复超时与警告；高级模式仍按用户显式保存的 DNS 草稿生成。
  高级草稿仍保存在配置中但不参与生成。schema v1～v3 缺少该字段时迁移为
  `advanced`，保持升级前实际 DNS 行为，不能静默套用新推荐值。
- `dns_servers`、`default_nameserver`、`proxy_server_nameserver`、`direct_nameserver`：高级模式下
  分别承担普通查询、DNS 服务域名引导解析、代理节点域名解析和 direct 出口解析。每组最多 8 个，
  接受 IP、`system`、普通主机名或 `https/tls/quic/dhcp/udp/tcp` DNS 地址；拒绝 URL 用户信息、
  query、fragment、控制字符和超长值，避免把凭据混入配置或形成不可审计上游。
- `dns_enhanced_mode`：`fake-ip` 或 `redir-host`；`dns_respect_rules` 控制 Mihomo
  `respect-rules`。`fake_ip_range` 必须是 `198.18.0.0/15` 内前缀 16～30 的 IPv4 子网；
  `fake_ip_filter[]` 最多 100 个域名。`redir-host` 运行配置不输出 fake-IP 地址池和排除项，但保留
  草稿，切回后继续使用。
- `nameserver_policy[]`：最多 100 条 `{domain, servers[]}` 域名后缀策略，域名不可重复；运行时
  转换为 Mihomo `nameserver-policy` 的 `+.<domain>` 映射。UI 每行采用
  `domain = server1, server2`，独立“验证 DNS 配置”只做本地规范化，不访问网络；最终保存仍必须
  经过完整 routing 预检和 Mihomo 配置检查。
- `controller_port` / `controller_secret`：仅监听回环地址的后端控制端，由 Python 控制面持有。
  UI 配置与所有路由结果会递归移除这两个字段，UI 草稿预检或应用时由后端合并当前私有值，
  避免缺字段导致 Secret 轮换。浏览器不再直连 Controller，因此运行配置不开放
  `external-controller-cors`；Controller 继续 `allow-lan=false`、绑定 `127.0.0.1` 并要求随机
  Bearer secret。
- `rules[]`：`id`、`enabled`、`match_type`、`domain`、`outbound`。

规则严格按“自定义 bypass → 启用的用户 `rules[]` → 本地规则包 → 可选 `GEOIP,CN` → `MATCH`”输出：
`exact` → `DOMAIN`，`suffix` → `DOMAIN-SUFFIX`，`wildcard` → `DOMAIN-WILDCARD`。
后端拒绝协议、端口、路径、
空域名和重复的“匹配方式 + 域名”。国际化域名在保存时转为 IDNA。停用规则仍保留在
`rules[]` 中，只在生成 Mihomo 规则、出口引用校验和命中解释时跳过。全局模式忽略用户规则和
内置规则，但显式 bypass 和启用的国内 IP 兜底仍保留在 `MATCH` 之前；重复的完全相同规则会按
首次出现位置合并。

## 配置保护与诊断

- `core/config_maintenance.py` 负责本地备份、恢复预览、路由应用历史和诊断脱敏；配置与历史
  均位于当前用户 LocalAppData。导入文件最大 2 MB，必须声明产品和备份版本，解析和路由
  规范化通过后才能进入应用事务。
- 默认备份不包含订阅 URL；用户显式勾选后可包含 URL，但 `creds`、手机号、授权状态、邮箱密码、
  VLM key、Controller 端口/secret 和自定义订阅上游始终不导出。恢复以当前内存配置为基底，
  保留所有本机凭据；未携带 URL 的 provider 按 ID 复用当前地址。恢复备份不改变当前
  `routing.enabled`，避免仅导入配置便意外接管或关闭系统流量。
- 恢复不是直接覆盖 JSON：UI 先调用 `preview_config_restore` 展示通用字段、路由字段、订阅和规则
  差异，用户再次确认后才复用 `apply_routing` 的“服务应用 → 原子保存 → 保存失败回滚服务”事务。
- 最近 12 次应用结果保存在
  `%LOCALAPPDATA%\CXVPNTools\routing\history`；旧版
  `%LOCALAPPDATA%\CXVPNManager\routing\history` 在首次启动时合并迁移。
  成功记录含完整路由配置以支持回退，失败记录
  只含摘要和脱敏错误；API 只向 UI 返回摘要。首次应用前先写入当前有效基线，历史回退本身也会
  先建立当前基线并产生新的应用记录。
- 诊断包是 UTF-8 JSON，包含平台、路由/规则/订阅数量摘要、原生服务诊断、最近应用与 Mihomo
  日志以及连接数量/流量合计。它不包含活动连接目标、进程路径或规则载荷；输出再次递归移除 URL、
  Bearer、查询凭据、常见 secret 字段和用户主目录。诊断导出不读取或修改真实网络状态。

## 出口模型

- `physical` 不是 Mihomo 内置的无约束 `DIRECT`，而是名为 `PHYSICAL`、显式绑定
  `physical_interface` 的自定义 `direct` 出口，确保不借道任意 VPN。
- 每个 `vpn:<name>` 生成独立 `direct` 出口并绑定 Windows 接口别名。目标 VPN 未连接时
  该出口失败，不回退到物理网络或代理，避免公司域名泄漏。
- `proxy` 使用全部已启用订阅公开组组成的 `PROXY` 组；`proxy:<id>` 使用对应的独立
  `PROXY-<id>` 组。聚合组只在 `PROXY-<id>` 之间执行全局 `url-test`、`fallback` 或 `select`，
  不得直接 `use` 原始 provider，否则会绕过订阅级智能优选、手动节点和故障转移策略。
  每个 provider 对节点名添加 `[订阅名] ` 前缀，避免不同订阅同名节点冲突。现有第三方 Clash
  不作为上游 TUN；应关闭其 TUN，直接把订阅交给本模块。
- `url-test` 每 300 秒检测并以 80ms 容差自动择优；`fallback` 按节点顺序选择首个可用
  节点。订阅进入手动模式时对应组固定生成为 `select`，持久化的是不含 `[订阅名] ` 前缀的
  原节点名；调用 Controller 时再恢复运行态前缀。手动选择只允许当前安全节点快照内的节点，
  Controller 仍只监听回环地址并要求 Bearer secret。节点偏好与路由出口相互独立：保存手动
  节点或切回自动优选不会隐式修改 `default_outbound`；网络代理首页启停、规则/全局切换同样
  不改写默认出口。
- 订阅智能优选由 `core/routing_auto_policy.py` 编译。`auto_policy.stages[]` 最多 8 个，
  每项包含地区代码、最多 12 个地区补充关键词和最多 12 个线路优先关键词；关键词按
  字面量安全转成 Go RE2。UI 用逐个添加/删除的标签编辑器维护数组，不接受在同一输入框
  以逗号、竖线或换行批量分隔。
  每个地区的 `selection_mode=latency` 时，“线路优先组”和“地区全部节点组”分别使用
  `url-test`，再用 `fallback` 保证线路层级；`selection_mode=failure` 时必须保存一个
  `preferred_node`，该节点、线路组和地区组均使用保持顺序的 `fallback`，只有当前候选故障
  才向后切换。地区组之间始终用 `fallback` 保证国家顺序。
  `latency_tolerance_unit=ms` 时范围为 0～500；`percent` 时范围为 0～100，并在生成配置时
  按安全节点快照中候选组的最低有效延迟换算成 Mihomo 原生毫秒容差。候选组无测速时回退
  订阅最低延迟，仍无数据时按 100 ms 基准换算。最终可 `reject`，也可增加全部节点兜底；
  全部地区均为仅故障模式时，全部节点兜底也使用 `fallback`。内部组设为 `hidden`，Controller
  概览会递归解析 `now` 到真实节点。
  运行中修改智能策略属于组结构变化，UI 确认连接可能短暂中断后复用完整应用事务立即
  重载；代理未开启时仅持久化，并在下次开启时应用。
- `block` 映射到 Mihomo `REJECT`。
- `PHYSICAL` direct 出口和 provider 节点的 `override.interface-name` 固定为物理接口；
  不设置全局 `interface-name`，避免本机 Clash HTTP 代理等回环连接被错误绑定到物理网卡。
  已选 VPN 服务器地址会尽量预解析并写入 `route-exclude-address`，降低 TUN 内递归风险。
- TUN 在 `strict-route=true` 下必须同时启用 `auto-detect-interface=true`。节点套接字继续使用
  provider `override.interface-name` 绑定物理接口，而自动探测负责 Mihomo 自身 DNS、Controller
  等内部连接的真实出口；关闭自动探测会让内部连接再次进入 TUN，形成路由递归并导致整机断网。
- 两种模式都启用 `unified-delay` 与 `tcp-concurrent`。运行配置由固定字段生成，用户不能注入
  Merge/Script 覆盖 `mixed-port`、`tun`、Controller、provider、策略组、DNS 或 rules。
- TUN fake-IP 同时配置 IPv4 `198.18.0.1/16` 与 IPv6 `fdfe:dcba:9876::1/64`，并保持
  `ipv6=true`；系统代理模式关闭 TUN，但复用同一规则、出口和 DNS 分层。

## 订阅下载出口

- `auto`：智能自动更新。每个 HTTP provider 生成隐藏的
  `SUBSCRIPTION-UPDATE-<id>` fallback 组，该组 `use` 同一 provider，且
  `empty-fallback: PHYSICAL`。Mihomo 启动时先加载 provider path 的旧缓存，因此已有缓存时
  用旧节点更新同一订阅；首次无缓存时组为空，先走物理网络取得第一份节点。独立预览或 GUI
  后台更新未产出节点时，如果检测到 Windows 手动系统代理，会再以显式 `system-proxy` 出口
  重试一次；成功结果标明“Windows 系统代理自动回退”，但不会把订阅配置改成固定系统代理。
- `physical`：provider 的 `proxy` 固定指向 `PHYSICAL`，明确绕过系统代理和 VPN 默认路由。
- `system-proxy`：固定使用检测到的 Windows 手动系统代理；未检测到时预检失败，不静默
  改走其他出口。只用于首次迁移或缓存节点全部失效后的恢复，不是默认依赖。
- `custom-proxy`：固定使用用户填写的本机 HTTP 代理，仅接受 `127.0.0.1`、`::1` 或
  `localhost`，不接受账号密码。只用于首次迁移或故障恢复。
- 系统代理读取 `HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings`
  的 `ProxyEnable` / `ProxyServer`。支持单一地址和 `http=...;https=...` 形式，不执行
  PAC/WPAD。只有显式恢复模式会记录保存/应用当时的代理端点。
- 独立预览与运行态更新都使用 Mihomo proxy-provider 原生 `proxy` 字段、相同 User-Agent、
  筛选器和下载出口，防止“预览能获取、服务更新失败”或反向不一致。自动模式主路径不依赖
  外部客户端；仅在主路径未产出节点时读取 Windows 手动系统代理作为一次性恢复出口。若该
  代理正由 Clash Verge 或其它本地代理客户端提供，可完成首份节点引导。
- `auto_update=true` 时，服务关闭且 GUI 打开期间由 `RoutingUpdateWorker` 按订阅 `interval`
  更新；服务启用后由 Mihomo provider 自身按同一间隔更新。关闭自动更新时不向 Mihomo 写入
  provider `interval`，但始终保留用户主动“更新订阅”的能力。

## 订阅缓存与首次种子

- `core/subscription_store.py` 在
  `%LOCALAPPDATA%\CXVPNTools\routing\providers` 保存
  last-known-good provider YAML 和不含订阅 URL 的元数据。文件名由 provider ID 与 URL
  SHA-256 前 16 位组成，URL 变化后旧缓存不会被加载。
- 同目录 `<provider>.yaml.nodes.json` 保存节点展示与测速快照。未设置筛选时沿用 URL 指纹；
  设置筛选后按“URL + filter + exclude_filter”签名校验，修改筛选会安全失效旧节点，避免旧手动
  节点绕过新筛选。快照原子替换，只允许节点名、展示名、类型、延迟、可用状态和测速时间字段，不保存服务器、端口、
  UUID、订阅 URL 或协议连接参数；损坏、越界或指纹不匹配时按空快照处理。
- 下载只有在 Mihomo 已解析出节点后才替换缓存；YAML 正文和元数据先分别写入同目录
  临时文件，再作为一对提交。任一步失败都用提交前快照恢复正文和元数据；远端失败、内容损坏
  或超过 10 MB 时保留上一份缓存。元数据中的节点数和时间戳使用有界整数解析，损坏值不会
  阻断配置加载。在线更新失败并回退旧缓存时不刷新 `updated_at`，自动更新线程可在下一轮继续
  重试。YAML 导入把正文、元数据和节点快照作为三个文件的同一回滚事务提交。删除订阅或保存
  新 URL 后清理不再引用的当前用户缓存。
- 服务配置应用时把匹配的当前用户缓存复制到 ProgramData `data/providers`。如果服务缓存
  更新更晚则不以旧的用户缓存覆盖；服务目录 ACL 仍只允许 LocalSystem 和 Administrators。
- 支持导入 UTF-8 Clash/Mihomo YAML 作为首次种子。导入文件先以 `file` provider 执行
  `mihomo -t`，然后进入正常内置预览链路；原始 vmess/vless 分享链接不属于该能力。
- 首次没有任何缓存且订阅 URL 无法物理直连时，不存在可用于下载自身的代理节点。智能自动
  更新会复用已检测到的 Windows 手动系统代理；未检测到或代理不可用时，必须导入 YAML，
  或显式指定自定义本地代理迁移。种子建立后即可卸载 Clash Verge。

## Clash Verge Rev v2.5.2 对照基线

对照源码固定为只读目录 `C:\develop\workspace\clash-verge-rev-reference`，tag `v2.5.2`，
commit `28f2efc504059b1dc75c793618b775c8e1b2a5f1`。以下结论只描述该版本源码，不把 UI 中的
“关闭系统代理”误写为 Mihomo 已停止：

- `src-tauri/src/utils/resolve/mod.rs` 先初始化 `CoreManager`，再初始化系统代理和代理守卫；
  `src-tauri/src/core/manager/mod.rs`、`lifecycle.rs` 会直接启动 service 或 sidecar 核心，启动条件
  不读取 `enable_system_proxy`。`src-tauri/src/feat/proxy.rs` 的开关只修改 Windows 系统代理接管，
  不停止 Mihomo。因此 Clash Verge 关闭系统代理后，Controller、已加载配置和本机
  `mixed-port` 仍可用。
- 已存在远程配置的手动更新由 `src-tauri/src/feat/profile.rs::perform_profile_update` 执行：先按
  配置当前选项请求；失败后强制 `self_proxy=true`，通过 Clash 自身的本机代理重试；仍失败再
  以 `with_proxy=true` 读取 Windows 系统代理重试。`src-tauri/src/config/prfitem.rs` 把
  `self_proxy` 映射为 `ProxyType::Localhost`，`src-tauri/src/utils/network.rs` 将其解析到
  `127.0.0.1:<mixed-port>`。所以 Windows 系统代理关闭并不妨碍第二步复用常驻核心内的已有节点。
  首次导入新 URL 是另一条 `from_url` 调用链，不应把已有配置更新的三段回退无条件套用到首次导入。
- `src/services/delay.ts` 通过本地 Mihomo Controller 对当前已加载节点调用 provider
  healthcheck 或普通 proxy delay；不会下载或更新订阅。批量测试最多 10 路并发，默认目标为
  `http://cp.cloudflare.com/generate_204`。因此 Clash 的“关闭代理后仍可测速”依赖常驻核心和
  已加载节点，并不表示在没有任何节点数据时也能测速。
- `src/hooks/use-system-proxy-state.ts` 对系统代理开关先乐观更新前端配置，再串行提交最终状态；
  `src-tauri/src/feat/config.rs` 只更新对应的系统代理或 TUN 配置，不在开关调用链中执行节点
  delay/healthcheck。测速结果由 `src/services/delay.ts` 独立维护，不能作为开关可点击或启动成功
  的前置状态。

2026-09-01 复核 GitHub 官方 release 后，`v2.5.2` 仍是最新稳定版，因此本地固定 tag 可继续作为
当前功能对照基线。与 CXVPN 后续工作区拆分直接相关的事实如下：

- 一级导航把首页、代理、订阅、连接、规则、日志和设置分开。CXVPN 已据此保留
  “网络代理”作为启停和状态首页，并把订阅、规则、连接观测从原“域名分流”复合页拆成独立
  工作区；运行日志与代理请求日志职责不同，不应合并成同一种记录。
- `profiles.tsx` 支持 URL 导入、新建本地配置、更新全部、批量选择/删除、拖拽排序、查看运行配置、
  异常数据强制刷新，以及 Merge/Script 链式增强。CXVPN 可复用批量更新、运行配置预览和明确的
  异常恢复入口；不得直接引入任意 Merge/Script 注入，以免绕过固定字段生成和安全校验。
- `proxies.tsx` 与 v2.5.2 release 支持代理组筛选、排序、延迟测试、快速定位、粘性分组和链式代理。
  CXVPN 已有节点筛选、排序、测速和定位，应补齐大量节点下的虚拟化/粘性分组；链式代理需要新的
  出口模型和环路校验，不属于订阅/规则/日志导航改造的默认范围。
- `connections.tsx` 区分活动连接和已关闭连接，支持搜索、排序、列表/表格视图、字段管理、连接详情、
  清空历史和关闭活动连接；`logs.tsx` 负责 Mihomo 实时日志，支持级别筛选、搜索、暂停、清空和
  正倒序。CXVPN 当前连接页提供目标主机、进程、规则、规则载荷、代理链和流量，
  通过后端 WebSocket 中继保护 Controller Secret，并使用有界内存而非默认永久落盘。
- Clash 的 `rules.tsx` 只展示和搜索当前运行规则，不直接修改内置规则。CXVPN 如需可编辑体验，
  内置包仍保持只读；编辑动作实现为禁用指定内置规则并生成用户规则覆盖，避免版本升级覆盖用户
  修改，同时保留恢复官方默认值的路径。
- v2.5.2 在页面不可见时暂停 Mihomo WebSocket 订阅，并修复连接页内存泄漏和日志强制滚到底部。
  CXVPN 的连接/日志页面必须按可见性启停前端订阅、在用户离开底部时停止自动滚动，并对缓冲区
  设置硬上限；不能用高频轮询替代日志流。
- DNS 简单/高级模式、托盘快捷操作、固定全局快捷键和轻量模式已按本地安全边界实现。WebDAV、
  任意启动脚本、外部 Controller 和
  允许局域网连接会扩大凭据或攻击面，不随首轮信息架构改造引入。

## 托盘、全局快捷键与轻量模式

- `DesktopController` 的托盘菜单在每次展开时调用 `Api.desktop_quick_snapshot`，只读取当前 routing
  配置和 `.nodes.json` 安全快照。菜单展示期望启用状态、规则/全局模式及每个已启用 provider
  最多 24 个节点，不访问 Controller、不更新订阅，也不返回订阅 URL、服务器或协议凭据。
- 托盘启停调用 `desktop_toggle_routing`，规则/全局切换调用 `desktop_set_traffic_mode`，节点切换调用
  `desktop_select_node`；三类动作都复用 `_apply_routing_locked` 的预检、服务事务、磁盘原子提交和
  历史记录。节点切换只接受当前持久化快照中的已启用订阅节点，耗时操作在后台线程执行，结果
  通过托盘气泡反馈，不阻塞 WinForms UI 线程。
- “打开连接页”先恢复主窗口，再由 pywebview 执行现有 `goToPage('connections')`。托盘或快捷键
  修改配置后发出 `cxvpn:desktop-action`，前端刷新已应用状态但保留正在编辑的 routing 草稿，避免
  高频操作静默丢失未保存内容。
- 全局快捷键使用独立 Win32 消息线程和 `RegisterHotKey`，固定为 `Ctrl+Alt+P` 启停代理、
  `Ctrl+Alt+M` 切换规则/全局、`Ctrl+Alt+C` 打开连接页，默认关闭。三项必须全部注册成功才视为
  active；任一组合被占用时撤销已注册组合并通过托盘提示，不引入键盘钩子或第三方监听依赖。
- 轻量模式默认关闭。开启后前端禁用动画、模糊和高成本阴影，后端 `UiStateStream` 采样间隔从
  0.5 秒调整为 1.5 秒；业务状态变化仍可通过 `poke()` 立即唤醒，连接和日志 WebSocket 原有的
  页面可见性暂停边界不变，因此不会牺牲代理启停、错误回滚或安全事件响应。

## Clash Verge 对照后的当前边界（2026-09-08）

- 订阅 URL 归一化兼容部分面板误把 query 拼进 path 的 `path&token=...` 形式；仅在没有正式
  query 且参数可解析时迁移，缓存 URL 指纹随后按归一化结果计算。缓存状态读取在文件清理竞态
  下按不可用处理，自动更新可以继续选择其它出口或保留旧缓存。
- 节点测速对 Mihomo 延迟字段做有限数值归一化：允许数字字符串和有限浮点，拒绝 `bool`；
  proxy-provider 节点同时识别 `provider_name` 与 `provider` 字段，结果保留单节点
  `elapsed_ms` 供运行态进度和诊断使用，安全节点快照仍只持久化白名单字段。
- 系统代理快切继续由原生服务事务负责；Python 侧同时核对服务返回的 `runtime_mode`、
  `system_proxy_active` 与 Windows 注册表最终值。状态不一致会尽力切回 standby，再向 UI 报错，
  不把 IPC 已响应误报为系统代理已生效。
- 启动复核还会将持久化 `routing.enabled` 与 Windows 实际手动代理端点对账：配置应启用但
  CXVPN `mixed-port` 未恢复时补做接管；配置已关闭但端点精确指向当前 `mixed-port` 时关闭残留。
  非 CXVPN 端点不被自动修改，避免误伤其它代理软件。
- 当原生服务已确认写入代理地址但交互会话仍回读 `ProxyEnable=0` 时，控制面由当前用户进程
  补写启用位，再在有限窗口内复核最终端点；失败仍回滚为待机并报告异常。

CXVPN 自原生路由服务 `0.3.0`、IPC 协议 `3` 起采用与上述基线一致的生命周期分层：

- 原生服务通过 `enabled.marker` 和 UTF-8 `runtime-mode` 持久化 `active`、`standby`、`stopped`
  三态。`active` 表示核心运行并按配置启用 TUN 或 Windows 系统代理，`standby` 表示核心运行但
  系统接管关闭，`stopped` 表示 Mihomo 不运行。旧版本只有 `enabled.marker` 时按 `active` 迁移，
  避免升级期间把既有运行态误判为待机。
- 用户关闭代理且满足快切条件时，控制面发送 `set_system_proxy_enabled(false)`，只恢复 Windows
  原始系统代理并让完整核心进入 `standby`。快切不可用时才发送 `stop_runtime`，确认接管停止并成功
  保存配置后立即向 UI 返回；存在已启用 provider 时，由后台复核任务在同一把 `_routing_lock` 内
  事务应用待机配置。任务开始前重新读取配置，若用户已重新开启代理则跳过；失败保持 `stopped`，
  不得回滚成已接管状态或阻塞关闭交互。
- Python 对回环 Controller 的所有 HTTP 请求必须使用显式空 `ProxyHandler`，不能使用会读取或缓存
  Windows/环境代理的默认 `urllib` opener；否则关闭系统代理后的就绪探测可能进入待机 mixed-port，
  再错误命中 `PROXY` 形成回环，并耗尽 Controller 等待上限。
- 原生服务 v0.4.0 / IPC protocol v4 支持 Windows 系统代理快切。服务对经过完整候选事务验证的
  system-proxy 配置维护 `fast-toggle-ready.marker`；关闭只把运行模式写为 `standby` 并恢复系统
  代理快照，开启只把模式写为 `active` 并重新写入 mixed-port，Mihomo 子进程始终运行。服务启动、
  子进程崩溃和系统关机仍按运行模式恢复或清扫系统代理，快切不能绕过原有残留保护。
- 原生服务 v0.5.0 / IPC protocol v5 随 `apply` 接收规范化后的入口绕过域名，并保存为服务目录
  `system-proxy-bypass.json`。该文件与 Mihomo 配置、数据、运行模式和快切标记处于同一候选事务，
  commit 后保留，rollback、超时和服务异常恢复时还原旧版本；服务重启、系统代理快切和接管状态
  对账都从该文件重建 `ProxyOverride`。Rust 服务再次限制数量、校验 ASCII/标签长度并拒绝分号、
  星号等注入字符，不能只信任 Python 规范化结果。
- 控制面快切开启需已确认运行配置签名、服务配置 SHA-256 与目标一致，且核心运行、服务版本兼容、
  快切标记存在且未熔断。开启前回读并校正手动节点，验证当前 mixed-port 链路，再写入并回读系统代理；
  配置变化、TUN、旧精简待机配置或异常状态自动回落完整事务。
- 首页开关调用只接收布尔启用状态和可选流量模式，后端基于最新持久化配置构造候选；应用结果立即
  更新前端内存状态，VPN/网卡/TUN 和节点详情的完整 setup 仅后台刷新。不得在普通开关前后同步执行
  两次 `get_routing_setup`，否则即使系统代理已切换，按钮仍会被 PowerShell 慢扫描阻塞。
- 待机配置始终关闭 TUN、禁止 LAN、只绑定 `127.0.0.1`，保留带 secret 的回环 Controller 和本机
  `mixed-port`。system-proxy 模式保留完整 VPN 出口、规则、DNS、provider 和代理组，使普通开启
  无需重载；TUN 模式仍使用不生成 VPN 出口或用户域名规则的精简待机配置。系统流量因未写系统代理
  且未启用 TUN 而不受影响，所有已启用 provider 在两种待机配置中都可更新和测速。
- 订阅手动更新优先通过常驻 Controller 的 provider `PUT` 执行。`auto` provider 已加载旧缓存时，
  其 `SUBSCRIPTION-UPDATE-<id>` 会复用当前 provider 节点，达到关闭系统代理后自代理更新的效果；
  更新完成后服务通过仅当前用户 SID 可访问的 Named Pipe `read_provider` 返回完整 provider YAML，
  控制面将其事务写回当前用户 last-known-good，再持久化不含凭据的节点快照。
- GUI 关闭期间的自动更新先查询 `core_running`；待机核心存在时走常驻 Controller，不存在时才按需
  启动临时 Mihomo。`setup.proxy_groups` 在 `core_running=true` 时可读取，因此 UI 能使用常驻节点
  直接测速，但 `status.running` 只在系统接管真实生效时为真；待机态单独以 `standby=true` 展示，
  不得提示为“代理已开启”。
- 应用启动后后台执行一次待机复核：仅当配置关闭、存在已启用 provider 且原生服务已经安装时，
  才升级或恢复待机核心；服务从未安装时不申请 UAC，继续使用临时核心。复核任务不得阻塞首屏，
  日志覆盖提交、领取、跳过、成功和失败。
- 临时订阅预览仍作为未安装服务、待机失败和未保存草稿的降级路径。`auto` 主路径会使用匹配 URL
  的完整 provider YAML 缓存尝试自举，并在主路径抛出 `ProviderFetchError` 后尝试已检测到的
  Windows 手动系统代理。若最后只是返回旧缓存，结果中的 `used_cache=true`、
  `refreshed=false`、`update_state=cache_retained` 表示远端更新没有成功，不得把“节点仍能显示”
  提示成“订阅已更新”。
- `test_preview_proxy_provider` 固定使用 `_cache_only=true`、`_refresh_cache=false`，把已有完整
  provider YAML 作为 `file` provider 加入临时核心，只测试当前缓存节点，不会重新拉取订阅。
  `.nodes.json` 是去除服务器、端口和凭据后的 UI 安全快照，不能单独用于建立代理连接；测速必须
  有 URL 指纹匹配的完整 provider YAML。没有完整缓存时应提示“尚无可测速的节点缓存”，不应把
  订阅下载失败伪装成测速失败。
- 已保存且启用的订阅在待机或接管核心运行时，UI 的“重新获取订阅”优先调用常驻 provider
  `PUT`，复用当前核心内的旧节点访问订阅地址；常驻更新失败后继续尝试临时内置代理、物理网络
  和已检测的 Windows 系统代理。未保存草稿、停用订阅或核心未运行时直接使用临时预览。不得
  因为系统代理关闭或运行组列表短暂未加载就跳过可用的待机核心。
- 临时预览的所有远端出口均失败后，如果 URL/筛选签名匹配的完整 last-known-good 仍可解析，
  后端显式以 `file` provider 读取它并返回 `cache_retained`。此降级不再次访问远端、不覆盖完整
  缓存或安全快照，并按节点名合并原快照的 `delay/alive/tested/tested_at`，避免把历史测速状态
  降级为未测速。只有 provider `PUT` 完成且回读到非空新节点后才能设置 `refreshed=true`。
- 首次没有完整缓存、订阅又无法物理直连，且 Windows 系统代理或用户指定的本机代理均不可用时，
  CXVPN 与 Clash 都不可能凭空获得首个可用节点。可行入口只有恢复外部代理、导入完整
  Clash/Mihomo YAML，或使用可直连的订阅地址。

所有订阅和测速结果使用独立状态：`remote_updated` 表示远端请求成功并取得节点，
`cache_retained` 表示远端失败但旧缓存仍可读取，`cache_tested` 表示只对当前完整缓存测速且没有发起
订阅请求，`cache_imported` 表示用户导入的完整 YAML 已成为种子。UI 不得用同一个成功或失败提示
合并这些状态。

## 启用门控与生命周期

启用/更新的顺序不可绕过：

1. 规范化配置并校验订阅 URL、域名和出口引用。
2. 只对实际被默认出口或启用规则引用的手动订阅确认 `selected_node` 仍存在于当前筛选签名
   对应的节点快照；未承载流量的手动订阅不阻止物理/VPN 配置启用。
3. 查找物理默认接口；仅当选择 TUN 时检测其它正在承载默认路由的 TUN 冲突。
4. 对每个 VPN 出口确认配置存在，且 IPv4、IPv6 远程默认网关都已关闭。
5. 生成 JSON（YAML 合法子集），用锁定版本的 `mihomo.exe -t` 解析。
6. 控制面比较包内与已安装 `CXVPNRoutingHost.exe` 的 SHA-256；仅首次安装或摘要变化时通过
   固定参数的 `ShellExecuteExW(runas)` 更新服务。订阅 URL、节点名和配置正文不进入命令行。
7. 控制面把配置、匹配的 provider 缓存和系统代理入口绕过域名以定长消息帧发送到 Named Pipe。
   服务端限制请求、配置、单个 provider 和绕过域名数量，校验文件名、SHA-256 与域名格式，在
   ProgramData 事务目录锁外复制旧数据并执行有界 `mihomo -t`；回到状态锁复核准备标识后应用。
   v0.7.0 / protocol 6 支持仅规则、模式和节点组变化时保留核心热重载；其余变化启动新子进程。
   两条路径均维护事务 ID、候选配置与入口绕过旁车文件，详见代理控制与运行状态文档。
8. Python 用带 Bearer secret 的本地 Controller 等待所有实际引用 provider 出现真实节点；
   `REJECT` 不算就绪，手动模式还要 PUT 并回读确认目标节点。随后只确认实际承载流量的代理组
   当前选择存在且属于该组，不重复执行节点 delay/healthcheck。节点测速是独立诊断，不是启动
   门控；真正的启动门控是下一步的端到端接管链路。成功发送 `commit`，任何失败发送
   `rollback`；客户端崩溃或 90 秒未提交时服务自动恢复旧配置和旧运行状态。服务进程异常退出后
   也会从 `pending.json` 恢复未完成事务。
9. commit 前，系统代理模式先用显式 `127.0.0.1:mixed_port` 做多端点探测，再由服务在未提交事务内完整快照
   `ProxyEnable`、`ProxyServer`、`ProxyOverride`、`AutoConfigURL`，写入安全 bypass 和逐域名的
   `domain;*.domain` 入口绕过、关闭 PAC，
   回读注册表确认与刚验证的 mixed-port 一致；TUN 模式验证系统 DNS/HTTPS 链路。各链路使用有限端点
   和共用期限，前两个端点并行。端到端链路不可用时先 rollback，再读取原生服务脱敏诊断，
   不能以 Controller 就绪代替公网可用。
   诊断日志可能包含中文和国旗等非 BMP 节点名，文件与 IPC 均保持 UTF-8；控制台不支持字符时
   只转义显示。日志与诊断是 best-effort 旁路，任何编码或读取异常都不得阻断 rollback。
   Python 默认 `urllib.request.urlopen` 会继承 Windows 系统代理；VLM 等应用内请求如需完全绕开
   `mixed-port`，应把其 API 主域名加入入口绕过，而不能仅依赖 Mihomo 内部 `PHYSICAL` 规则来改变
   DevTools 或客户端看到的远程地址。
10. 系统代理事务失败、超时、rollback、关闭运行时或 Windows Service 停止时恢复四字段原值。
   Mihomo 单次异常退出先恢复系统代理再重启；60 秒内连续退出 3 次删除运行标记并熔断，防止
   死代理。服务重启会根据运行标记和快照恢复运行时；未完成事务仍恢复旧版本。
11. 服务成功后，以同目录临时文件、`fsync` 和 `os.replace` 原子保存 `config.json`；若保存
   失败，`Api.apply_routing` 重新应用原 routing 配置作为补偿回滚。提交时从内存最新配置合并
   routing，避免覆盖操作期间发生的其它配置更新。

Windows 服务状态读取失败时使用 `installed=None`、`state=Unknown`，不得推断为“未安装”。
关闭或保存关闭状态必须先确认服务状态；未知时安全失败，避免服务继续运行但配置显示关闭。
关闭时通过 IPC 删除运行标记并停止 Mihomo，但保留 Windows Service 与 ProgramData 文件，
供下次免 UAC 启动；只有旧版 WinSW 服务在迁移前关闭时仍会注销。普通
`Api.save_config` 必须保留后端现有 `routing`，只有 `Api.apply_routing` 可以修改它。
提权操作通过随机临时结果文件把子进程异常回传给控制面，读取后立即删除并按统一规则脱敏；
Controller、provider 和手动选点等已知错误转换为用户可操作提示，不能统一归因于 UAC。
Windows PowerShell 5.1 不得使用 `Invoke-RestMethod` 直接解析 Mihomo 的节点 JSON：响应未必声明
UTF-8 `charset`，中文和国旗节点名会按系统代码页变成乱码。提权事务统一使用
`HttpWebRequest`，请求正文编码为 UTF-8 字节，响应从原始流用 UTF-8 `StreamReader` 解码后
再 `ConvertFrom-Json`；节点就绪比较、PUT 切换和回读确认必须共用该路径。
新版配置、provider 缓存和节点信息全部经 Named Pipe 传输，命令行只包含固定的安装参数、当前
用户 SID 和随机结果文件路径，从架构上消除 `WinError 206`。旧版 GZip PowerShell 事务仅保留
在 WinSW 迁移回退代码中，不再用于新版配置更新。

## 下拉组件约束

- 域名分流页不得直接展示 Windows/WebView 原生 `select` 菜单。`ui/routing.js` 的
  `enhanceRoutingSelect` 保留隐藏的原生 `select` 作为值模型，触发器和选项由应用内组件
  渲染；顶部字段使用名称 + 说明双行密度，动态规则行使用单行紧凑密度。
- 菜单追加到 `document.body` 并使用 fixed 定位，避免被卡片和规则容器裁切；每次打开按
  触发器矩形、上下可用空间和视口边距决定展开方向，最大高度 300px，溢出后内部滚动。
- 动态重建规则前必须调用 `destroyRoutingSelects` 移除 portal 菜单，避免删除/排序规则后
  留下孤立 DOM。刷新接口和 VPN 列表后调用组件 `refresh`，同步当前文本和选中态。
- 键盘必须支持方向键、Home/End、Enter/Space、Escape 和 Tab；触发器维护
  `aria-haspopup`、`aria-expanded`、`aria-controls`，选项维护 `role=option` 和
  `aria-selected`。原生 `select.disabled` 必须同步到可见触发器和 portal 选项；预览、保存
  等忙碌状态期间不得继续打开或修改下拉值。

## 网络代理与分流控制台界面

- 侧栏“网络代理”是日常使用入口，由 `ui/proxy.js` 管理：首页提供一键启停、当前订阅、
  当前节点、真实缓存摘要，并独立展示系统代理/TUN 接管、规则/全局策略和默认出口。首页可从当前目标订阅
  已持久化的安全节点快照中提取明确格式的剩余流量、重置时间和到期日；缺失时隐藏，公告不计入，且不会
  为展示触发远程更新。实时上/下行只读取 Mihomo `/traffic` WebSocket 的真实字节速率；除此之外
  不展示后端尚未提供的伪造总量、百分比或连接时长。
- `core/mihomo_telemetry.py` 管理实时流量生命周期。UI 只通过
  `set_routing_telemetry_active(bool)` 提交页面可见意图；后端使用 Authorization header 连接回环
  Controller `/traffic`，离页、进入节点页、窗口隐藏或服务停止后关闭。断线按 1/2/4/8/15 秒
  退避，断开期间速率归零，单个损坏帧被忽略但不终止后续恢复；`ui/routing_telemetry.js` 只负责
  格式化后端快照，不包含 WebSocket、Controller 地址或 secret。遥测任务为页面激活状态维护
  单调生命周期版本；离开再返回会废弃旧连接或退避等待，禁止被上一轮最长 15 秒延迟阻塞。
- `/connections` 含目标域名/IP，不允许直接进入 WebView。后端遥测任务每 10 秒读取并在 Python
  内脱敏聚合为活动连接数和规则命中摘要，再与实时速率一起进入全局版本化状态流；实时帧不写日志，
  通道日志只记录请求提交、后台领取、connecting、connected、reconnecting、idle 和停止终态，
  不含 URL、Authorization 或 secret。
- 首页状态机为 `off`、`running`、`degraded`、`unknown`。`degraded` 表示配置已启用但 Controller
  未就绪，可重试启动或关闭配置；`unknown` 表示 Windows 服务状态不可确认，只允许重新读取，
  不执行启停推断。
- GUI 开启后会主动预取两阶段状态。`get_routing_bootstrap` 只做配置规范化、本地 provider
  缓存/节点快照读取、Windows 系统代理注册表读取和最多 600ms 的原生服务 IPC，
  不枚举 VPN、物理网卡或 TUN 冲突，也不等待 Controller 探测。该快照先驱动真实
  订阅、节点和策略首屏，完整 `get_routing_setup` 后台补齐运行态、接口、冲突和
  可观测数据；进入网络代理首页只触发保留草稿的后台刷新，不得因为 15 秒 TTL 过期把全局
  `busy` 传播到主启停按钮。首次尚未取得任何本地快照时，按钮只等待快速 bootstrap。
- 网络代理首页、节点卡片和智能优选下拉框的节点状态必须读取当前已应用订阅签名
  对应的运行态代理组；服务未运行时回退到持久节点快照，不得只用 provider YAML 缓存行数推断。
  运行组测速完成后回写 `provider_nodes`，保证延迟、`tested`、`alive` 在各入口一致。订阅流量、重置、
  到期等 metadata 不计入可选节点；“缓存记录”仍保留原始记录数，两者可以不同。
- 首页启停必须先用已读取快照完成选择门控并立即显示确认框，用户确认后再执行
  完整状态复核和 `apply_routing`。首次安装、使用 TUN 等网络接管不使用“危险操作”
  视觉；只有不可逆或高破坏性操作使用红色确认。
- “查看全部节点”进入一级节点工作台，复用分流模块的运行态代理组和独立订阅
  预览能力，支持订阅组筛选、节点搜索、只看可用、后台整组测速、停止测速和持久化选点。
  地区筛选由节点国旗和中英文地点名称在前端动态推导，只展示当前组实际存在的常用地区；排序支持
  订阅原始顺序、当前节点优先、延迟、倍率和名称，均不改变后端节点顺序或持久化配置。“定位当前
  节点”会清除展示筛选并聚焦手动目标或运行节点。
  已启用订阅的节点卡可“设为本订阅节点”，各订阅独立保存手动偏好，不改写默认出口。
  未启用订阅的手动选点按钮禁用，前端提交入口与后端 `save_proxy_preference` 同时拦截；
  后端以已保存订阅的启用状态为准，不能通过传入 enabled 草稿绕过。
  节点卡区分“本订阅已选”“订阅当前节点”和“默认出口当前节点”：默认出口标记以
  已保存配置的 `default_outbound` 对应运行组解析出的真实节点为依据，聚合出口读取 `all`
  组；不把其它订阅组的选中值或未应用出口草稿当作默认出口。代理关闭或运行组缺失时不标记
  默认出口当前节点。预览节点比对时补回订阅名前缀，避免跨订阅同名节点混淆。
  订阅也可切回自动优选；从网络代理快捷入口进入时可返回
  “网络代理”，全部入口始终复用同一节点状态，避免两套节点列表并存。
- 节点工作台的“优选策略”打开内联智能策略面板；默认草稿为“日本 → 美国”，每个地区
  默认优先 `高速专线/IPLC/IEPL`。用户可调整地区顺序、添加补充匹配词、分别设置线路词、
  选择延迟容差和最终兜底，界面同步输出执行流程。动态地区 `<select>` 必须立即通过
  `enhanceRoutingSelect` 增强，切换订阅时关闭面板并清理挂载到 `body` 的浮层。
- 一键启用仍走 `apply_routing`，不依赖 Clash Verge；首次安装/升级原生服务时申请 UAC，之后
  日常启停通过 IPC 完成。关闭 GUI 后 Service 和已启用的 Mihomo 继续工作。只有实际出口引用
  代理时才要求可用订阅，首页不得替用户重设 `default_outbound`。

- 一级侧栏把“网络代理、订阅、节点、规则、连接、日志”拆成独立工作区；网络代理只承担启停、
  状态和快捷入口。高级分流概览仍承担引擎开关、物理接口、DNS、聚合代理池策略和动态配置检查；
  默认出口、本地规则包和域名命中测试位于规则工作区。规则页必须展开当前规则文件的实际离线规则，
  支持按匹配值搜索及按域名/IP 类型筛选，并展示 `PHYSICAL` 出口和 `no-resolve` 等选项；全局模式下
  明确说明本地规则保留但不参与当前运行。规则明细在界面中只读，文件可在当前用户 AppData 中编辑；用户
  规则仍在规则包之前匹配。
- 订阅、规则和高级分流不是三份配置：`RoutingWorkspace` 维护唯一的 `routingConfig`、
  `appliedConfig`、dirty 状态和保存栏，`routing_workspace.js` 只把现有 DOM panel 投放到对应一级页面。
  页面切换不得重新创建草稿，也不得因状态 TTL 到期覆盖未保存修改。
- 页面头部把产品说明、运行状态和刷新操作合并为一个紧凑工作台；概览指标可直接跳转到
  对应规则、订阅或节点页。底部保存栏同时承担未保存状态、操作反馈和预检/应用入口。
- 订阅卡默认折叠，只展示启用状态、策略、更新周期、节点可用数和当前节点；订阅 URL、
  包含/排除正则等低频字段仅在编辑态展示。订阅 URL 在编辑态直接明文显示且不提供显隐开关，
  但日志、诊断包和默认配置备份仍不得泄露该地址。修改尚未保存时不得用 Controller
  更新旧订阅，必须先“保存并应用”。
- 每张订阅卡提供“查看节点”入口，进入节点页时优先选择该订阅对应的 `PROXY-<id>` 组。
  未保存或服务未运行时，“预览节点”会启动不含 TUN 和代理监听端口的临时 Mihomo，加载
  last-known-good 后尝试更新并解析当前草稿订阅；provider YAML 与安全节点展示快照会持久化，
  URL 和筛选签名一致时下次打开 GUI 可直接恢复节点展示，但不会改变 Windows 路由。
- 新建订阅默认是停用的未验证草稿；填写 URL 并完成预览后再由用户决定是否启用。分流加载使用
  单例 in-flight Promise 和 15 秒短时 TTL；`appliedConfig`、`draftConfig`、`runtimeStatus`
  分开维护，网络代理首页只按已应用配置显示，刷新高级页时可保留未应用草稿。
- 网络代理首页以已保存的 `appliedConfig` 为基线说明下次开启会采用的接管方式、流量策略和默认出口，
  但允许首页当前选择的规则/全局模式在本次开启时覆盖对应字段；高级分流存在未保存草稿时必须显示
  常驻提示，并在开启确认框再次说明除首页流量模式外，草稿不会自动进入本次运行配置。用户需返回
  高级分流显式“保存并应用”，禁止首页启停隐式提交其他草稿。
- 临时订阅预览使用页面选择的物理接口和 DNS，并按当前订阅的下载出口获取内容；自动模式
  通过内置缓存节点更新，无缓存时回退物理网络，主路径未产出节点时再尝试已检测的 Windows
  手动系统代理。临时 Controller 只监听回环
  地址、使用随机 secret，并在读取 provider 前校验 `/version`。Mihomo 子进程加入
  `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 的 Windows Job，主程序异常退出时也会被系统回收。
- 初次预览只证明订阅可下载且节点格式可解析，节点页先把结果标为“未测速”。用户可在节点页
  执行“临时全部测速”：控制面以相同物理接口和 DNS 临时启动不含 TUN、监听代理端口和
  Windows 服务注册的 Mihomo。测速必须先存在 URL 指纹匹配的 last-known-good，并把缓存作为
  `file` provider 加载，不携带订阅 URL、不会顺带刷新订阅；随后通过本地 Controller 的
  `PREVIEW` 组逐节点测试，最多 8 路并发；每完成一个节点即更新后台任务状态和节点卡，支持
  用户停止。provider 节点使用
  `GET /providers/proxies/<provider>/<node>/healthcheck`，不再假定 provider 节点存在于新版
  Controller 的通用 `/proxies` 目录；停止后保留已收集结果，不等待或发布迟到请求。
  默认测速目标与 Clash Verge Rev v2.5.2 对齐为
  `http://cp.cloudflare.com/generate_204`，超时 10 秒且要求 HTTP 204，避免 Google HTTPS
  目标的地理路径和 TLS 握手造成跨客户端延迟不可比。测速后立即终止临时进程并保存安全节点快照。
  订阅未启用时仍可预览和测速，但不能手动选点；已启用订阅在代理关闭时可保存节点偏好，
  不会隐式开启代理或修改默认出口。启用服务时后端再次校验并应用。关闭引擎或重启 GUI 后仍可读取
  签名一致的持久快照。
- 名称表现为流量、重置、到期、公告等订阅说明的伪节点单独放入“订阅信息”，不参与节点计数、
  可用数、测速、搜索结果选点和代理卡片；国旗字符在 Windows WebView 中转换为 ISO 双字母徽标。
  未测速节点使用 `alive=None`，显示为“未检测/尚未测速”；只有 `tested=true`
  且 `alive=false` 才显示“本次检测未通过/不可用”。Controller history 的 `time` 转换为
  `tested_at` 并随安全快照持久化；首页显示延迟时必须同时展示测速时效，不能把历史延迟表达为
  当前启动可用性。启动只确认选点一致性并以真实接管链路探测为门控，不重复测速当前节点。
- proxy-provider 请求默认设置 Clash 兼容 `User-Agent`（每个订阅可覆盖）和 10 MB 下载
  上限；订阅 URL 和底层未知异常不得进入运行日志或直接返回 UI。
- 代理节点页按代理组展示节点名称、类型、可用状态和最近延迟，支持搜索、只看可用及后台
  整组测速。前端通过 `start_*_routing_test` 创建任务，再轮询 `get_routing_test_job`；任务提供
  总数、完成数、可用数、失败数和逐节点状态。没有
  节点组时隐藏筛选工具栏并提供直达订阅管理的操作，避免展示不可执行的控件。
  测速任务带版本号，未变化返回 unchanged；可见节点页 750 ms、隐藏页面至少 2 秒读取。
  卡片按节点名复用，仅内容签名变化时重建子元素；进度刷新不重建订阅表单。
- 运行态整组测速取消后停止提交新节点，只收集已经完成的结果，并与测速前完整节点清单合并，
  未完成节点不会从快照消失；配置变化导致的过期任务不能落盘。单节点测速将对应延迟、状态和时间合并到
  该订阅快照。前端进度轮询采用指数退避；持续失败时显式请求取消，取消状态未知则继续低频
  查询，不把仍在运行的后台任务伪装成已失败。
- `RoutingManager.proxy_overview` 通过 Controller `/proxies` 读取策略组，再通过
  `/providers/proxies` 合并新版 Mihomo 不再放入通用目录的 provider 节点类型、可用状态和
  延迟；旧版 Controller 不支持 provider 目录时回退通用节点信息。订阅立即更新使用
  `PUT /providers/proxies/<name>`，provider 节点测速使用
  `GET /providers/proxies/<provider>/<node>/healthcheck`，普通内联代理才使用
  `GET /proxies/<name>/delay`，手动节点应用仍使用 `PUT /proxies/<group>`。运行态更新后同步
  安全快照；若原选择节点被移除，接口返回 `selection_invalid` 并在下次开启前强制重选。
- 运行态订阅 `PUT` 更新后必须重新读到目标代理组且节点非空才确认成功；Controller 查询失败时
  返回“无法确认”并保留界面原节点列表。订阅说明节点的前后端识别规则保持一致，普通名称中仅
  出现“流量”等关键词而没有说明格式时仍作为真实节点测速。
- 用户可见错误只描述当前已验证的失败事实、已执行的恢复动作和下一步操作。重试次数、测试端点、
  `dns/timeout/proxy/network` 分类以及“并非某原因”等排查性结论只进入日志；除非产品流程确实
  需要用户在多个状态间选择，否则不得把历史假设或反证写入 toast/常驻提示。网络代理首页已有
  常驻 `role=alert` 时不再用 toast 重复展示同一长错误。
- 订阅预览和常驻 provider 更新的失败分支必须保留 `providerPreviews`、`setup.proxy_groups`、
  `setup.provider_nodes` 与磁盘 last-known-good；错误只附加到当前订阅状态。失败后节点工作台仍
  展示上次节点和可用数，并明确提示“远端更新失败，已保留当前节点列表”。只有 URL、筛选签名
  变更或用户明确删除订阅时才允许失效旧预览。
- 域名命中测试调用 `explain_domain`，严格复现规则顺序、精确/后缀/通配符语义及默认出口，
  只做解释，不访问目标域名、不修改系统路由。结果以 `user`、`builtin`、`default` 标明来源，
  同时标明 VPN/订阅出口当前是否可用。启用国内直连时，静态解释无法解析目标 IP，因此未命中
  域名规则的结果明确标为运行时 `GEOIP(CN)` 判断，而不伪装成已经确定直连或代理。
- 网络代理首页的轻量遥测从 Controller `/connections` 读取去标识化活动连接数、上下行累计值和
  规则类型聚合，只进入全局 UI 状态流，不返回目标域名或 IP。一级“连接”页另由
  `MihomoActivityRelay` 通过 `/connections` WebSocket 中继当前活动连接与本次页面会话内最近关闭
  记录，字段限于目标主机/IP/端口、进程文件名、规则、规则载荷、代理链、流量和开始时间；支持
  搜索、排序、清空关闭历史以及通过后端 `DELETE /connections[/<id>]` 关闭连接。
- 网络代理首页复用轻量遥测维护最近 60 秒的上下行速率采样，以同一纵轴绘制 SVG 趋势图；
  代理未连接、正在连接或重连时清空采样并归零，避免把上一会话流量误显示为当前速率；
  图表悬停层按最近采样点显示时间、当前节点及上下行速率，焦点态也可读取最新采样。
- 一级“日志”页把 `/logs?level=debug` 的 Mihomo 核心日志与原程序运行日志分开。核心日志支持级别、
  搜索、暂停、清空和正倒序；Controller secret、订阅 URL、URL 凭据、敏感查询参数和 Bearer token
  在进入 UI 前脱敏，核心日志默认只保存在内存，不写入 `run.log`。
- `MihomoActivityRelay` 同一时刻只维护当前可见的连接或日志 WebSocket；页面离开、切换到程序日志、
  窗口隐藏或退出时切回 `idle`。前端用版本游标调用 `wait_routing_activity` 阻塞等待变化，不把连接/
  日志数组塞进 0.5 秒全局状态快照。活动连接、关闭历史和核心日志硬上限分别为 1000、500、1000；
  用户滚离日志底部时新日志不强制抢夺滚动位置。
- 页面底部保存栏在存在未保存修改时保持可见，并提供放弃修改；重新读取配置前必须提示会
  丢弃修改，窗口关闭时使用 `beforeunload` 防止无提示退出。
- 当统一分流服务从未安装且引擎保持关闭时，“保存配置”只校验并持久化草稿，不申请 UAC；
  首次启用或服务二进制摘要变化时申请管理员权限，日常更新与关闭不再申请。
- 订阅卡需区分独立预览、运行中和“运行服务仍使用已应用配置”三种状态。停用或删除订阅前，
  UI 必须检查默认出口和规则中的 `proxy:<id>` 引用；停用或删除最后一个已启用订阅时还要检查
  即将失效的聚合 `proxy` 引用。用户确认后，有其它已启用订阅则把受影响出口迁移为聚合
  `proxy`，否则迁移为 `block`，避免代理流量静默回退物理网络。删除订阅还要迁移停用规则中的
  直接引用，不能留下以后启用规则时才暴露的无效 ID，也不能等到预检才报错。

## 安全与打包

- 启动前按固定 SHA-256 校验 `mihomo.exe`、离线 `Country.mmdb` 和旧版恢复用 `WinSW-x64.exe`；
  原生服务安装或升级把数据库复制到服务 data 目录并再次校验，配置预检也使用同一快照；运行时
  不下载或静默更新地理数据。原生服务升级则比较
  包内与运行中服务自身报告的 SHA-256。版本和摘要记录在
  `runtime/routing/README.md`。
- `core/routing_support.py` 集中处理运行时路径、SHA-256 和 Mihomo 错误脱敏。返回 UI 的底层
  错误移除 HTTP(S) URL、Authorization、token、secret、password 和控制字符；预检配置只写入
  系统临时目录并随上下文删除。
- Mihomo 控制端 `allow-lan=false`、绑定 `127.0.0.1` 并启用随机 secret。
- ProgramData 服务目录只授予 LocalSystem 和 Administrators 完全控制，避免普通用户替换
  以 LocalSystem 运行的服务二进制；Named Pipe DACL 只允许 LocalSystem、Administrators 和
  安装时记录的当前用户，并拒绝远程客户端。订阅 URL 不进入命令行。
- Mihomo 与 MetaCubeX meta-rules-dat 为 GPL-3.0，WinSW 与原生服务所链接 Rust 库使用 MIT 或兼容双许可证；打包目录必须
  包含 `THIRD_PARTY_NOTICES.md`、`LICENSE-MIHOMO.txt`、`LICENSE-WinSW.txt` 和
  `LICENSE-RUST-MIT.txt`。
- `build.py` 将整个 `runtime/routing` 放入 PyInstaller `_internal/runtime/routing`；运行时
  通过 `sys._MEIPASS` 解析，源码模式从仓库同名目录解析。`build.py` 与
  `build_protected.py` 都先调用 `build_runtime.build_routing_service()`；Rust 源码有变化时执行
  release 构建并更新 `CXVPNRoutingHost.exe`，目标机不需要 Rust 工具链。
- 两种打包方式都把仓库 `rule-packs/*.txt` 作为只读默认资源放进 `_internal`；运行时将缺失文件
  补到 `%LOCALAPPDATA%\CXVPNTools\rule-packs`。打包和启动迁移都不覆盖用户手工维护的域名。

## 已知边界

- 内置运行时目前仅 Windows x64。
- 订阅必须能被 Mihomo 作为 proxy-provider 解析；只有分享链接而非 Clash/Mihomo
  provider 内容的订阅需要先转换。
- 系统代理接管仅设置当前用户的手动代理，不执行 PAC/WPAD；启用前保存 PAC URL，停止或失败
  后原样恢复。其它软件当前占用系统代理时，只有用户明确应用系统代理接管才会临时替换。
- 如果缓存内全部节点失效，且订阅 URL 又无法物理直连，智能自动更新会尝试检测到的 Windows
  手动系统代理；该代理也不可用时仍需导入新的 Clash/Mihomo YAML，或指定可用本地代理恢复。
- 目标 VPN 可以在服务运行后连接，但连接前命中流量会失败；若 VPN 服务器域名无法预解析，
  重连遇到问题时需暂时关闭统一分流。
- fake-IP 可保留原始域名用于分流。依赖公司专用 DNS 的内部域名需要配置可达 DNS；按域名
  分配不同上游 DNS 的 UI 尚未开放。
