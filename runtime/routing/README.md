# 统一分流运行时

此目录包含 Windows x64 统一分流运行时。Mihomo 从固定官方源码加本项目补丁构建，
其余第三方资源来自官方发布；均校验 SHA-256。

| 文件 | 版本 | SHA-256 | 来源 |
| --- | --- | --- | --- |
| `mihomo.exe` | v1.19.30-cxvpn.2 (GOAMD64=v1) | `5187346B49D7CEC1E6B2C4A8C02E648720BC37E7CF889B0E9783A4787618436B` | MetaCubeX/mihomo + patches/mihomo |
| `Country.mmdb` | country-lite 快照（2026-09-02） | `4BF15C30737F7CC2807BCBE1ACE44149B18579BEA3B13BF7BEA935A3F2834052` | MetaCubeX/meta-rules-dat |
| `WinSW-x64.exe` | v2.12.0 | `05B82D46AD331CC16BDC00DE5C6332C1EF818DF8CEEFCD49C726553209B3A0DA` | winsw/winsw |

`CXVPNRoutingHost.exe` 0.8.1 是本项目从 `routing-service/` 构建的第一方 Windows Service，
负责 Named Pipe IPC、配置事务、当前用户系统代理快照恢复、崩溃熔断和 Mihomo 子进程生命周期。`WinSW-x64.exe` 仅保留用于
从旧版服务迁移失败时恢复，不再承担新版日常启停。

Mihomo 对应修改后源码随包提供于 `mihomo-source.zip`，构建记录见 `mihomo-build.json`。
仓库内 `python build_mihomo.py --install` 可重建；日常打包只核对摘要，不下载或重编核心。
补丁边界与构建说明见 `patches/mihomo/README.md`。

上游地址：

- https://github.com/MetaCubeX/mihomo/releases/download/v1.19.30/mihomo-windows-amd64-compatible-v1.19.30.zip
- https://github.com/MetaCubeX/meta-rules-dat/releases/tag/latest
- https://github.com/winsw/winsw/releases/tag/v2.12.0

Mihomo 与 meta-rules-dat 使用 GPL-3.0 许可证；WinSW 使用 MIT 许可证。分发要求与版权说明见
`THIRD_PARTY_NOTICES.md`。
