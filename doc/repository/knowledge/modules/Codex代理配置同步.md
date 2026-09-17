# Codex 代理配置同步

模块：Codex 本机代理、模型传输配置与桌面重启
关键词：`CODEX_HOME`、`config.toml`、`mixed_port`、手动启停、WSS、HTTPS/SSE、自定义 API、API Key、`experimental_bearer_token`、`supports_websockets`、`model_provider`、快照恢复、TOML 原子写入、`OpenAI.Codex`、MSIX、AppsFolder、重启 Codex

代码路径：`core/codex_proxy.py`、`core/codex_runtime.py`、`core/codex_session_provider.py`、`api.py`、`ui/index.html`、`ui/app.js`

最后验证：2026-09-17 | 分支：main

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
- Codex 页面另有独立的「优先 WSS」开关。未启用自定义 API 时，开启表示配置选择恢复为内置 `openai`，允许 Codex 使用默认传输策略并在失败时回退；关闭表示配置选择切到本工具托管的 `cxvpn_openai_http`，该 Provider 使用 `https://chatgpt.com/backend-api/codex`、`wire_api = "responses"`、`requires_openai_auth = true` 和 `supports_websockets = false`。启用受托管自定义 API 后，开关直接修改 `codex_local_access.supports_websockets`，不再切换根级 Provider。
- 传输开关只编辑 `CODEX_HOME/config.toml` 或默认的 `%USERPROFILE%\\.codex\\config.toml`，不依赖 Codex CLI，也不读取 `auth.json` 或 Token。配置选择为内置 `openai` 或本工具已托管 Provider 时可切换；存在可信恢复记录时，即使根级选择已恢复或已改成其他 Provider，也可直接开启「优先 WSS」来恢复原选择并清理未被修改的专用 Provider。`openai_base_url`、活动 profile、对内置 `openai` 的覆盖、同名未托管 Provider、无可信记录的冲突状态仍保持原样并返回具体原因。
- `Api.set_codex_websocket(enabled)` 严格接收布尔目标值，避免重试时反向切换。`get_codex_status()` 返回 `transport_mode`、`websocket_enabled`、`transport_managed`、`transport_conflict`、`transport_restore_available`、`transport_cleanup_pending`、`active_provider` 和事务阶段；这些字段只描述解析到的用户配置，不代表已运行进程的实际 Provider 或网络连接结果。
- 传输恢复记录位于用户数据目录的 `codex_transport_snapshot.json`，与代理快照分离，只保存原根级 Provider 是否存在及原值、专用 Provider 所有权、最后写入值和 `prepared/committed` 阶段。该文件是外部 `config.toml` 的最小恢复记录，不是活动偏好的第二数据源。
- 传输编辑仅支持可证明安全的根级 bare `model_provider` 与完整 `[model_providers.cxvpn_openai_http]` table。候选 TOML 会解析并与原结构对比，只允许目标字段变化；写入前回读原文防止覆盖外部编辑，恢复时只移除未被用户修改的专用 Provider。
- Codex 页面提供独立的「自定义 API」配置区。保存时只写入 `codex_local_access`，包含用户输入的 `base_url`、`wire_api = "responses"`、`requires_openai_auth = true`、`supports_websockets` 与 `experimental_bearer_token`，根级 `model_provider` 也指向 `codex_local_access`。`requires_openai_auth` 让 Codex 保持 ChatGPT 登录身份；当前 Codex CLI `0.155.0-alpha.2.6` 的本机回环探针确认模型请求仍使用 `experimental_bearer_token`，不会改用 ChatGPT OAuth bearer。旧版受托管配置缺少该字段时，页面提示再次保存升级。
- 自定义 API 配置只接管 `codex_local_access`；保存动作本身不改写历史任务。用户确认执行「重启 Codex」后，应用完全退出期间会把侧边栏仍可见的根任务同步到 `config.toml` 当前根级 `model_provider`，因此账号模式与自定义 API 模式可双向切换。归档任务、子 Agent、内部任务、无预览或首条用户消息、无官方 SQLite 引用，以及 rollout 无法安全核对的任务保持不变。
- API Key 输入框默认以密码形式回显，用户可通过显隐按钮查看或直接编辑；首次保存必须输入。保存成功后，地址和 API Key 写入 SQLite 主配置的 `codex_api`，API Key 不复制到兼容 `config.json`、配置导出、通用 UI 配置响应、运行日志、恢复记录或状态摘要。独立回显接口优先读取 SQLite；升级后 SQLite 尚无值时，可从仍受托管的 Codex Provider 读取一次并迁移入库。
- 「还原原配置」只恢复 Codex `config.toml` 及其恢复记录，不删除或修改 SQLite 中的 `codex_api`。还原后页面继续回显工具保存的地址和密钥，用户可直接再次保存并启用。
- 自定义 API 恢复记录位于 `%LOCALAPPDATA%\CXVPNTools\codex_api_snapshot.json`。当前自定义 API Key 不以明文写入恢复记录，只保存最后写入摘要用于一致性判断；为精确恢复保存前可能已存在的 `codex_local_access`，恢复记录会在本机保留该 Provider 的原始完整 table。还原时将未被手动修改的 `codex_local_access` 恢复为原 table，或在原先不存在时删除；手动修改过的 Provider 或根级选择会保留并返回警告。
- 版本 1 恢复记录包含早期单 `codex_local_access` 和过渡期 `cxvpn_custom_api` + `codex_local_access` 两种结构，按兼容字段是否存在区分。当前 Provider 与对应的公开字段及密钥摘要完全匹配时，允许回显、重新保存或修改 WSS，并升级为版本 2 单 Provider 记录；双 Provider 结构中旧别名已删除但兼容 Provider 仍匹配时，也视为可安全继续的半迁移状态。实际内容不匹配时仍判定冲突；无可信恢复记录的同名 `cxvpn_custom_api` 不自动删除，`openai` 和其他 Provider 始终不参与迁移。
- 自定义 API 与 `cxvpn_openai_http` 不会同时保留恢复事务。保存自定义 API 时会校验旧传输快照和专用 Provider 所有权并自动移交：当前仍选中专用 Provider 时恢复其原选择，当前已选中其他 Provider 时保留该选择，然后删除未被手动修改的专用 Provider 与旧快照。专用 Provider 被手动修改或恢复记录不可信时拒绝自动清理；自定义 API 恢复记录存在时，传输开关也不会创建另一份 Provider 恢复事务。
- Codex 页面提供需要二次确认的「重启 Codex」操作。`core/codex_runtime.py` 通过 `OpenAI.Codex` MSIX 安装目录精确筛选包内 `ChatGPT.exe` 进程，先请求主窗口正常关闭，超时后强制结束仍残留的包内进程；确认完全退出后同步历史任务 Provider，再通过包的 AppsFolder 标识启动并等待不带 `--type=` 的主进程。它不会按通用 `ChatGPT.exe` 或 `codex.exe` 名称结束进程；历史同步失败时仍尝试重新启动，并将结果标记为部分失败。
- 历史同步以 Codex 官方 `state_5.sqlite` 的 `threads` 行为入口，同时支持根目录和 `sqlite/` 子目录布局及 Windows `\\?\` 扩展路径。每个候选 rollout 必须位于 `sessions/`、文件名符合 `rollout-*.jsonl`、首行为合法 UTF-8 `session_meta`、任务 ID 与 SQLite 一致且不是子 Agent/内部来源。同步只修改首行的 `payload.model_provider` 和对应 `threads.model_provider`，不会遍历或改写其他事件内容。
- 写入前在 `%LOCALAPPDATA%\CXVPNTools\codex-session-provider-backups` 备份候选 rollout，并通过 SQLite `VACUUM INTO` 创建一致数据库备份；写入阶段校验 rollout 的大小和修改时间，SQLite 使用 `BEGIN IMMEDIATE` 且只更新扫描时 Provider 仍一致的行。任一步失败会恢复 rollout 和数据库并清理 WAL/SHM；成功后保留最近 3 份备份，并执行 `PRAGMA integrity_check`。
- 当前 Codex app-server experimental schema（CLI `0.155.0-alpha.2.6`）的 `ThreadResumeParams` 允许传入 `modelProvider`，但 `ThreadSettingsUpdateParams` 和 `ThreadMetadataUpdateParams` 没有 Provider 持久化更新字段。CXVPNTools 无法控制桌面端恢复旧任务时是否传 override，因此采用同步 rollout 与 SQLite 的持久化方案；这是基于本机版本协议的实现约束，不代表官方承诺的长期兼容接口。

## 页面交互约束

- 代理按钮与结果放在代理配置卡片内，重启与查看配置保留在底部操作区；卡片间距复用设置页的 16px 节奏，路径字段使用剩余空间，窄窗口切换为单列。
- 保存、还原、传输切换、代理启停及重启共享页面操作互斥。状态未读取成功时配置写入按钮禁用；成功、失败或取消后回读并恢复控件，避免重复提交及不同配置操作交叉执行。
- 地址与密钥有独立的未保存标记。后台回读不得覆盖用户草稿；保存失败保留草稿，保存或还原成功才清除标记。状态与密钥读取均检查请求序号，迟到结果不得覆盖较新的页面状态；离开页面收起密钥明文。
- 还原提示明确区分恢复 config.toml 的原 Provider 与重启时统一同步历史任务，不声称逐条恢复历史 Provider。重启已成功、历史同步失败时显示警告，不误报应用启动失败。
- HTTP/SSE 只描述传输方式，不代表加密。是否使用 TLS 取决于 API 地址的 HTTPS 配置，不能向 HTTP 地址显示“仅加密 HTTPS/SSE”的承诺。

## 运行边界

代理配置只影响读取 Codex 配置环境变量的进程；传输开关只管理模型 Responses 请求。内置浏览器、插件独立进程、内部通信和其他 WebSocket 连接不受传输开关控制。配置变化本身不会自动结束 Codex；只有用户在确认弹窗中明确执行「重启 Codex」才会关闭桌面应用、同步可见历史任务并重新启动。活动 profile 存在时拒绝迁移，因为根级 Provider 不能代表实际目标。文件值不能证明 WSS 已连接或 HTTPS/SSE 模型请求已成功。

## 离线验证

`test_codex_proxy` 覆盖缺失配置、局部更新、section 补齐、端口变更、幂等、关闭移除、手动修改保护、损坏 TOML、权限/写入中断、中文/UTF-8、read_config/status；`test_api_codex_proxy` 覆盖手动动态取端口、重启提示标记、查看日志脱敏、只读刷新/查看，以及关闭成功才清理、关闭失败/保存回滚不清理、清理失败不撤销软件代理关闭。

`test_proxycore` 在模拟 Windows 与原生服务的条件下断言底层路由操作不直接调用 Codex sync/restore，覆盖快切启停、启动残留修复、已有系统代理/TUN、完整事务及端口变更、完整停止。关闭联动由上层 API 提交成功后执行，原有 Windows 启用位补写、状态回读及用户会话刷新断言继续保留。

传输测试覆盖默认 WSS 状态、首次关闭、重复关闭幂等、恢复原根值或缺失状态、根选择已恢复后的残留清理、其他 Provider 下的可信恢复、保留其他 Provider、同名冲突、复杂 TOML、`CODEX_HOME` 变化、损坏或未完成事务、用户修改保护、写入失败、严格布尔参数、UTF-8 内容，以及代理与传输设置互不干扰。自定义 API 测试覆盖单一 `codex_local_access` 的地址、密钥和 WSS 修改，内置 `openai` 全流程不改写，原 `codex_local_access` 精确恢复，早期单 Provider 与过渡期双 Provider 两种版本 1 快照迁移，SQLite 密钥落盘与 Codex Provider 一次性迁移，兼容 JSON 和通用 UI 响应排除密钥，还原仅影响 Codex 配置，恢复快照不保存当前密钥明文，留空沿用密钥、地址规范化、旧传输事务自动移交、修改冲突拒绝和写入失败回滚。前端测试覆盖三态显示、本机保存状态、旧版迁移提示、可恢复开关、自定义 API 自动移交提示、处理中禁用、明确目标提交、密钥输入清空与失败后回读。

`test_codex_session_provider` 覆盖可见根任务筛选、归档与子 Agent 排除、Windows 扩展路径、双向切换、幂等、mtime 保留、活动 profile 拒绝、非法 UTF-8、任务 ID 不匹配、缺失配置默认 `openai`，以及 SQLite 写入失败后的自动回滚。`test_codex_runtime` 使用模拟 PowerShell 结果覆盖 MSIX 包与主进程筛选、AppsFolder 启动、非零退出、35 秒超时，以及迁移失败或异常时仍重新启动；API 与前端测试覆盖生命周期日志、确认弹窗、处理中禁用及成功反馈。离线测试不会实际结束 Codex，也不会改写真实历史任务。
