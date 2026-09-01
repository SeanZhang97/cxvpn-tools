# Windows 统一域名分流

模块：Windows 网络路由 | 入口：`core/routing.py`、`core/routing_service.py`、`Api.apply_routing`
界面：`ui/proxy.js`、`ui/routing.js`、`ui/routing_nodes.js`、`ui/routing_telemetry.js` | 原生服务：`routing-service/` | 运行时：`runtime/routing/`
关键词：Mihomo, Named Pipe, Windows Service, system-proxy, Proxy Guard, TUN, routing schema, 内置规则包, proxy-provider, Windows VPN, 节点筛选, 节点排序, 订阅流量, 套餐到期, WebSocket, 后端遥测中继, 版本化状态流, 实时流量, Clash Verge Rev, 常驻核心, mixed-port, 订阅引导
最后验证：2026-09-01 | 分支：非 Git 工作区

## 职责边界

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

## 配置与规则映射

`config.json.routing` 的稳定字段为：

- `enabled`：期望启用状态，默认 `false`。
- `schema_version`：当前为 `2`。旧配置缺少版本时按首版语义迁移为 `capture_mode=tun`、
  `builtin_rule_pack=off`，不得套用新安装默认值改变既有流量路径。
- `capture_mode`：`system-proxy` 或 `tun`，二者互斥。新安装推荐默认是
  `system-proxy`；TUN 是需要透明接管 UDP/不遵循系统代理应用时的高级模式。
- `traffic_mode`：`rule` 或 `global`。规则模式执行 `rules[]`；全局模式保留规则配置但生成
  运行配置时忽略域名规则，全部流量直接使用 `default_outbound`。切回规则模式后原规则恢复。
- `physical_interface`：物理直连绑定的 Windows 接口别名；留空时选择默认路由中优先级
  最高且非 VPN/TUN 的已连接接口。
- `proxy_strategy`：全部已启用订阅合并后的策略，支持 `url-test`、`fallback`、`select`。
- `proxy_providers[]`：最多 16 个 Clash/Mihomo proxy-provider；字段为 `id`、`name`、
  `url`、`enabled`、`strategy`、`interval`、`filter`、`exclude_filter`、
  `download_route`、`download_proxy`、`user_agent`、`selection_mode`、`selected_node`、
  `auto_update`。`selection_mode=auto` 按 `strategy` 自动择优；`manual` 固定使用
  `selected_node`。`auto_update` 默认关闭。首版的 `proxy_provider_url` /
  `proxy_provider_interval` 会在规范化时迁移为 `default` 订阅；旧订阅缺少新增字段时默认
  使用 `auto` 下载出口和 `Clash-Verge` User-Agent。
- `default_outbound`：`physical`、`proxy`（全部订阅）、`proxy:<订阅 ID>`、`block` 或
  `vpn:<Windows VPN 名称>`。
- `builtin_rule_pack`：`off`、`local-direct-v1`、`cn-direct-v1`。规则包随程序离线发布、
  只读且版本固定，不后台下载第三方大规则集；`cn-direct-v1` 由本地/私有地址、`.cn` 与
  少量长期稳定的国内基础服务域名组成。
- `mixed_port`：系统代理模式的本机 mixed-port，默认 `17890`，不得与 Controller 重复。
- `dns_servers`、`default_nameserver`、`proxy_server_nameserver`、`direct_nameserver`：
  分别承担普通查询、DNS 服务域名引导解析、代理节点域名解析和 direct 出口解析，避免节点
  域名走自身代理形成递归。
- `controller_port` / `controller_secret`：仅监听回环地址的后端控制端，由 Python 控制面持有。
  UI 配置与所有路由结果会递归移除这两个字段，UI 草稿预检或应用时由后端合并当前私有值，
  避免缺字段导致 Secret 轮换。浏览器不再直连 Controller，因此运行配置不开放
  `external-controller-cors`；Controller 继续 `allow-lan=false`、绑定 `127.0.0.1` 并要求随机
  Bearer secret。
- `rules[]`：`id`、`enabled`、`match_type`、`domain`、`outbound`。

规则严格按“启用的用户 `rules[]` → 只读内置规则包 → `MATCH`”输出：`exact` → `DOMAIN`，
`suffix` → `DOMAIN-SUFFIX`，`wildcard` → `DOMAIN-WILDCARD`。`global` 模式只输出 `MATCH`。
后端拒绝协议、端口、路径、
空域名和重复的“匹配方式 + 域名”。国际化域名在保存时转为 IDNA。停用规则仍保留在
`rules[]` 中，只在生成 Mihomo 规则、出口引用校验和命中解释时跳过。

## 出口模型

- `physical` 不是 Mihomo 内置的无约束 `DIRECT`，而是名为 `PHYSICAL`、显式绑定
  `physical_interface` 的自定义 `direct` 出口，确保不借道任意 VPN。
- 每个 `vpn:<name>` 生成独立 `direct` 出口并绑定 Windows 接口别名。目标 VPN 未连接时
  该出口失败，不回退到物理网络或代理，避免公司域名泄漏。
- `proxy` 使用全部已启用 provider 组成的 `PROXY` 组；`proxy:<id>` 使用对应的独立
  `PROXY-<id>` 组。每个 provider 对节点名添加 `[订阅名] ` 前缀，避免不同订阅同名节点
  冲突。现有第三方 Clash 不作为上游 TUN；应关闭其 TUN，直接把订阅交给本模块。
- `url-test` 每 300 秒检测并以 80ms 容差自动择优；`fallback` 按节点顺序选择首个可用
  节点。订阅进入手动模式时对应组固定生成为 `select`，持久化的是不含 `[订阅名] ` 前缀的
  原节点名；调用 Controller 时再恢复运行态前缀。手动选择只允许当前安全节点快照内的节点，
  Controller 仍只监听回环地址并要求 Bearer secret。节点偏好与路由出口相互独立：保存手动
  节点或切回自动优选不会隐式修改 `default_outbound`；网络代理首页启停、规则/全局切换同样
  不改写默认出口。
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

- `core/subscription_store.py` 在 `%LOCALAPPDATA%\CXVPNManager\routing\providers` 保存
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

CXVPN 自原生路由服务 `0.3.0`、IPC 协议 `3` 起采用与上述基线一致的生命周期分层：

- 原生服务通过 `enabled.marker` 和 UTF-8 `runtime-mode` 持久化 `active`、`standby`、`stopped`
  三态。`active` 表示核心运行并按配置启用 TUN 或 Windows 系统代理，`standby` 表示核心运行但
  系统接管关闭，`stopped` 表示 Mihomo 不运行。旧版本只有 `enabled.marker` 时按 `active` 迁移，
  避免升级期间把既有运行态误判为待机。
- 用户关闭代理时，控制面必须先发送 `stop_runtime`，由服务移除运行标记、终止旧 Mihomo 并恢复
  Windows 原始系统代理；确认系统接管已停止后，再事务应用待机配置。待机事务失败只返回警告并
  保持 `stopped`，不得回滚成已接管状态或阻止用户关闭代理。
- 待机配置关闭 TUN、禁止 LAN、只绑定 `127.0.0.1`，保留带 secret 的回环 Controller 和本机
  `mixed-port`；不生成 VPN 出口或用户域名规则，所有显式进入待机端口的流量走已有 `PROXY`，
  系统流量因未写系统代理且未启用 TUN 而不受影响。所有已启用 provider 都加载到核心，即使当前
  默认出口没有引用该订阅，也能更新和测速。
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
7. 控制面把配置和匹配的 provider 缓存以定长消息帧发送到 Named Pipe。服务端限制请求、配置、
   单个 provider 大小，校验文件名与 SHA-256，在 ProgramData 事务目录复制旧数据并再次执行
   `mihomo -t`；通过后停止旧子进程、换入候选并启动，返回事务 ID。
8. Python 用带 Bearer secret 的本地 Controller 等待所有实际引用 provider 出现真实节点；
   `REJECT` 不算就绪，手动模式还要 PUT 并回读确认目标节点。成功发送 `commit`，任何失败发送
   `rollback`；客户端崩溃或 90 秒未提交时服务自动恢复旧配置和旧运行状态。服务进程异常退出后
   也会从 `pending.json` 恢复未完成事务。
9. commit 前对每个实际承载流量的代理组读取当前节点并执行 provider healthcheck。系统代理
   模式先用显式 `127.0.0.1:mixed_port` 做多端点探测，再由服务在未提交事务内完整快照
   `ProxyEnable`、`ProxyServer`、`ProxyOverride`、`AutoConfigURL`，写入安全 bypass、关闭 PAC，
   回读注册表并再次验证系统代理链路；TUN 模式验证系统 DNS/HTTPS 链路。各链路使用有限端点、
   有界超时和 `dns/timeout/proxy/network` 分类。节点、DNS 或出口任一不可用时
   读取原生服务最近的脱敏 Mihomo 日志并 rollback，不能以 Controller 就绪代替公网可用。
   诊断日志可能包含中文和国旗等非 BMP 节点名，文件与 IPC 均保持 UTF-8；控制台不支持字符时
   只转义显示。日志与诊断是 best-effort 旁路，任何编码或读取异常都不得阻断 rollback。
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
  可观测数据；后台刷新不阻塞日常首页交互。
- 网络代理首页的“可选节点”和“节点检测”必须读取当前已应用订阅签名
  对应的持久节点快照，不得只用 provider YAML 缓存行数推断。订阅流量、重置、
  到期等 metadata 不计入可选节点；“缓存记录”仍保留原始记录数，两者可以不同。
- 首页启停必须先用已读取快照完成选择门控并立即显示确认框，用户确认后再执行
  完整状态复核和 `apply_routing`。首次安装、使用 TUN 等网络接管不使用“危险操作”
  视觉；只有不可逆或高破坏性操作使用红色确认。
- “查看全部节点”进入网络代理内的二级节点工作台，复用分流模块的运行态代理组和独立订阅
  预览能力，支持订阅组筛选、节点搜索、只看可用、后台整组测速、停止测速和持久化选点。
  地区筛选由节点国旗和中英文地点名称在前端动态推导，只展示当前组实际存在的常用地区；排序支持
  订阅原始顺序、当前节点优先、延迟、倍率和名称，均不改变后端节点顺序或持久化配置。“定位当前
  节点”会清除展示筛选并聚焦手动目标或运行节点。
  每个节点卡可“使用此节点”，订阅也可切回自动优选；节点二级页提供
  依据来源返回“网络代理”或原域名分流页签的入口；域名分流中的节点入口是工作台动作，不伪装
  成没有对应 panel 的 ARIA tab，避免两套节点状态并存。
- 一键启用仍走 `apply_routing`，不依赖 Clash Verge；首次安装/升级原生服务时申请 UAC，之后
  日常启停通过 IPC 完成。关闭 GUI 后 Service 和已启用的 Mihomo 继续工作。只有实际出口引用
  代理时才要求可用订阅，首页不得替用户重设 `default_outbound`。

- 分流配置分为“概览、分流规则、订阅管理”三个真实页签，并提供独立“打开节点工作台”动作。
  概览只承担引擎开关、
  物理接口、DNS、聚合代理池策略和动态配置检查；默认出口与域名命中测试位于规则页。
- 页签导航必须显式覆盖全局侧栏 `nav` 样式：使用四列横向网格（三个页签加一个工作台动作）、`flex-direction: row` 和
  非全宽按钮，避免继承侧栏纵向布局。页签使用 `tablist` / `tab` / `tabpanel` 语义，支持
  左右方向键、Home/End，并在切页后让主内容回到顶部。
- 页面头部把产品说明、运行状态和刷新操作合并为一个紧凑工作台；概览指标可直接跳转到
  对应规则、订阅或节点页。底部保存栏同时承担未保存状态、操作反馈和预检/应用入口。
- 订阅卡默认折叠，只展示启用状态、策略、更新周期、节点可用数和当前节点；订阅 URL、
  包含/排除正则等敏感或低频字段仅在编辑态展示。修改尚未保存时不得用 Controller
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
  Controller 的通用 `/proxies` 目录；已启动但尚未结束的并发任务在停止后仍收集结果，避免
  页面回显与最终快照不一致。默认测速目标与 Clash Verge Rev v2.5.2 对齐为
  `http://cp.cloudflare.com/generate_204`，超时 10 秒且要求 HTTP 204，避免 Google HTTPS
  目标的地理路径和 TLS 握手造成跨客户端延迟不可比。测速后立即终止临时进程并保存安全节点快照。预览态允许把节点
  保存为待启用目标；若订阅仍是未保存草稿，该操作会保存订阅草稿和节点偏好，但不会隐式
  启用订阅或修改默认出口。启用服务时后端再次校验并应用。关闭引擎或重启 GUI 后仍可读取
  签名一致的持久快照。
- 名称表现为流量、重置、到期、公告等订阅说明的伪节点单独放入“订阅信息”，不参与节点计数、
  可用数、测速、搜索结果选点和代理卡片；国旗字符在 Windows WebView 中转换为 ISO 双字母徽标。
  未测速节点使用 `alive=None`，显示为“未检测/尚未测速”；只有 `tested=true`
  且 `alive=false` 才显示“本次检测未通过/不可用”。启动 commit 前会对当前实际节点
  执行最多两次实时 provider healthcheck；该门控失败是当次实测失败，不得与未测速
  初始态混用，也不代表节点永久失效。
- proxy-provider 请求默认设置 Clash 兼容 `User-Agent`（每个订阅可覆盖）和 10 MB 下载
  上限；订阅 URL 和底层未知异常不得进入运行日志或直接返回 UI。
- 代理节点页按代理组展示节点名称、类型、可用状态和最近延迟，支持搜索、只看可用及后台
  整组测速。前端通过 `start_*_routing_test` 创建任务，再轮询 `get_routing_test_job`；任务提供
  总数、完成数、可用数、失败数和逐节点状态。没有
  节点组时隐藏筛选工具栏并提供直达订阅管理的操作，避免展示不可执行的控件。
- 运行态整组测速即使被取消，也只取消尚未开始的节点；已开始结果继续回收，并与测速前完整
  节点清单合并后保存，未完成节点不会从快照消失。单节点测速会把对应延迟、状态和时间合并到
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
- 域名命中测试调用 `explain_domain`，严格复现规则顺序、精确/后缀/通配符语义及默认出口，
  只做解释，不访问目标域名、不修改系统路由。结果以 `user`、`builtin`、`default` 标明来源，
  同时标明 VPN/订阅出口当前是否可用。
- 运行页从 Controller `/connections` 读取去标识化活动连接、上下行累计值和规则类型聚合；UI
  只展示连接数及 `user/builtin/default` 命中摘要，不返回目标域名、IP 或连接凭据。
- 页面底部保存栏在存在未保存修改时保持可见，并提供放弃修改；重新读取配置前必须提示会
  丢弃修改，窗口关闭时使用 `beforeunload` 防止无提示退出。
- 当统一分流服务从未安装且引擎保持关闭时，“保存配置”只校验并持久化草稿，不申请 UAC；
  首次启用或服务二进制摘要变化时申请管理员权限，日常更新与关闭不再申请。
- 订阅卡需区分独立预览、运行中和“运行服务仍使用已应用配置”三种状态。停用或删除最后一个
  已启用订阅时，除 `proxy:<id>` 外还要检查聚合 `proxy` 出口引用，不能等到预检才报错。

## 安全与打包

- 启动前按固定 SHA-256 校验 `mihomo.exe` 和旧版恢复用 `WinSW-x64.exe`；原生服务升级则比较
  包内与运行中服务自身报告的 SHA-256。版本和摘要记录在
  `runtime/routing/README.md`。
- `core/routing_support.py` 集中处理运行时路径、SHA-256 和 Mihomo 错误脱敏。返回 UI 的底层
  错误移除 HTTP(S) URL、Authorization、token、secret、password 和控制字符；预检配置只写入
  系统临时目录并随上下文删除。
- Mihomo 控制端 `allow-lan=false`、绑定 `127.0.0.1` 并启用随机 secret。
- ProgramData 服务目录只授予 LocalSystem 和 Administrators 完全控制，避免普通用户替换
  以 LocalSystem 运行的服务二进制；Named Pipe DACL 只允许 LocalSystem、Administrators 和
  安装时记录的当前用户，并拒绝远程客户端。订阅 URL 不进入命令行。
- Mihomo 为 GPL-3.0，WinSW 与原生服务所链接 Rust 库使用 MIT 或兼容双许可证；打包目录必须
  包含 `THIRD_PARTY_NOTICES.md`、`LICENSE-MIHOMO.txt`、`LICENSE-WinSW.txt` 和
  `LICENSE-RUST-MIT.txt`。
- `build.py` 将整个 `runtime/routing` 放入 PyInstaller `_internal/runtime/routing`；运行时
  通过 `sys._MEIPASS` 解析，源码模式从仓库同名目录解析。`build.py` 与
  `build_protected.py` 都先调用 `build_runtime.build_routing_service()`；Rust 源码有变化时执行
  release 构建并更新 `CXVPNRoutingHost.exe`，目标机不需要 Rust 工具链。

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
