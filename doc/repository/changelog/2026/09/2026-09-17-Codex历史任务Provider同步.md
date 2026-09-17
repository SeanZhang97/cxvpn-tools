# Codex 历史任务 Provider 同步

- 日期：2026-09-17
- 功能：重启 Codex 时将侧边栏可见历史任务同步到当前 Provider
- 分支：`main`

## 需求概述

Codex 的既有任务会保留创建时的 `model_provider`。只修改 `config.toml` 根级 Provider 后，新任务会使用自定义 API，但恢复旧任务仍可能消耗 ChatGPT 账号额度。参考 cockpit-tools 的启动前修复机制，在 Codex 完全退出后安全同步官方任务数据库与 rollout 元数据，使账号模式和自定义 API 模式可双向切换。

## 涉及文件

- `core/codex_session_provider.py`
- `core/codex_runtime.py`
- `ui/index.html`
- `ui/app.js`
- `tests/test_codex_session_provider.py`
- `tests/test_codex_runtime.py`
- `tests/test_ui_codex_transport.js`
- `doc/repository/knowledge/INDEX.md`
- `doc/repository/knowledge/modules/Codex代理配置同步.md`

## 关键设计

- 目标 Provider 读取 `config.toml` 根级 `model_provider`；配置缺失时按 Codex 内置默认值 `openai` 处理，存在活动 profile 时拒绝迁移。
- 只处理官方 `state_5.sqlite` 中未归档、有预览或首条用户消息、有 rollout 引用且不是子 Agent/内部来源的任务。rollout 必须位于 `sessions/`，首行是合法 UTF-8 `session_meta`，并与 SQLite 任务 ID 一致。
- 同步修改 rollout 首行的 `payload.model_provider` 与 SQLite `threads.model_provider`。rollout 采用流式复制和原子替换，保留原修改时间；SQLite 使用事务、写前值校验和完整性检查。
- 写入前备份全部候选 rollout，并用 `VACUUM INTO` 创建一致数据库备份。任一步失败自动恢复两类文件并清理 SQLite WAL/SHM；成功后保留最近 3 份备份。
- 重启流程拆为精确关闭、Provider 同步、重新启动三步。同步返回失败、抛出异常或返回无效结果时仍会重新启动 Codex，并明确返回部分失败。
- UI 只在用户确认执行「重启 Codex」后触发同步；保存自定义 API、页面刷新和打包启动 CXVPNTools 都不会直接改写真实 Codex 历史任务。

## 影响范围

- 只改写侧边栏仍可见且可安全核对的根任务。归档任务、子 Agent、内部任务、无官方数据库引用或元数据不一致的任务保持不变。
- 左下角 ChatGPT 登录身份由 `requires_openai_auth = true` 保留；历史任务 Provider 同步只决定模型请求使用当前账号通道还是当前自定义 API。
- 实现依赖 Codex 当前的 `state_5.sqlite` 与 rollout 元数据结构；遇到未知结构时跳过而不是猜测写入。

## 验证情况

- `uv run python -m compileall -q main.py api.py core`：通过。
- `uv run python -m unittest tests.test_codex_session_provider tests.test_codex_runtime tests.test_api_codex_proxy tests.test_api_state_stream tests.test_state_store_recovery tests.test_codex_proxy`：106 项通过。
- `node --check ui/app.js`、`node tests/test_ui_codex_transport.js`、`node tests/test_ui_polish.js`：通过。
- `uv run python -m tests.run_routing_offline`：293 项全部通过。
- `git diff --check`：通过，仅有工作区既有的 LF/CRLF 转换提示。
- 对真实 Codex 数据只执行只读预检，未主动重启 Codex 或写入历史任务。
- 只读预检目标为 `codex_local_access`：1 个官方数据库、52 条可见根任务、52 个 rollout 待同步、0 条跳过。
- `uv run --with pyinstaller python build.py`：标准目录包构建成功，产物为 `dist/CXVPNTools/`；构建前创建一致性主库备份，真实桌面数据视图复核 `integrity_check=ok`、revision 111，routing、authorization 与 codex_api 摘要保持一致，兼容 `config.json` 保留。
- 产物未携带 `state.sqlite3`、`config.json`、`auth.json`、Codex 快照或备份目录；新版 `CXVPNTools.exe` 已自动启动，核对时 PID 为 `26096`。

## 注意事项

- 当前 Codex app-server experimental schema 仅在恢复任务时提供可选 `modelProvider` override，没有持久化更新已有任务 Provider 的公开更新字段；桌面端是否传 override 不受 CXVPNTools 控制，因此仍需同步官方持久化元数据。
- 历史任务结构属于 Codex 本机实现细节。同步严格采用白名单筛选、备份和回滚，并在 schema 或元数据无法安全核对时跳过。
