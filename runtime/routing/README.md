# 统一分流运行时

此目录包含 Windows x64 统一分流所需的第三方运行时。二进制仅从项目官方
GitHub Release 获取，并在引入时校验 SHA-256。

| 文件 | 版本 | SHA-256 | 来源 |
| --- | --- | --- | --- |
| `mihomo.exe` | v1.19.30 compatible (GOAMD64=v1) | `6AC25FCB26AFE8E1BEA24B6E6E80805BF884A33232D12E2D78DFA0B6C529AC14` | MetaCubeX/mihomo |
| `Country.mmdb` | country-lite 快照（2026-09-02） | `4BF15C30737F7CC2807BCBE1ACE44149B18579BEA3B13BF7BEA935A3F2834052` | MetaCubeX/meta-rules-dat |
| `WinSW-x64.exe` | v2.12.0 | `05B82D46AD331CC16BDC00DE5C6332C1EF818DF8CEEFCD49C726553209B3A0DA` | winsw/winsw |

`CXVPNRoutingHost.exe` 0.6.1 是本项目从 `routing-service/` 构建的第一方 Windows Service，
负责 Named Pipe IPC、配置事务、当前用户系统代理快照恢复、崩溃熔断和 Mihomo 子进程生命周期。`WinSW-x64.exe` 仅保留用于
从旧版服务迁移失败时恢复，不再承担新版日常启停。

上游地址：

- https://github.com/MetaCubeX/mihomo/releases/download/v1.19.30/mihomo-windows-amd64-compatible-v1.19.30.zip
- https://github.com/MetaCubeX/meta-rules-dat/releases/tag/latest
- https://github.com/winsw/winsw/releases/tag/v2.12.0

Mihomo 与 meta-rules-dat 使用 GPL-3.0 许可证；WinSW 使用 MIT 许可证。分发要求与版权说明见
`THIRD_PARTY_NOTICES.md`。
