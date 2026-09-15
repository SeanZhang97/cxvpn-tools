# Mihomo Windows VPN route hook

基线固定为上游 `v1.19.30`，定制版本为 `v1.19.30-cxvpn.2`。
`apply.py` 对精确源码上下文应用最小连接钩子，`overlay/` 保存新增 Go 文件及测试。
不维护独立 DNS 或代理数据面。除明确标记 `cxvpn-managed-route` 的 direct 出口外不启用。

## 重建

在仓库根目录运行 `python build_mihomo.py --install`。
需要 Go，当前验证工具链为 `go1.27.0 windows/amd64`；固定 Windows amd64/v1、关闭 CGO，
保留上游 `with_gvisor`，使用固定版本和构建时间、`trimpath` 与空 build ID。
脚本验证源码归档 SHA-256，严格匹配补丁上下文，执行钩子测试，再构建与更新两个摘要常量。
下载超时 30 秒，相关测试 60 秒，核心编译 120 秒；失败不跳过检查。

产物为 `runtime/routing/mihomo.exe`、`mihomo-build.json` 与 `mihomo-source.zip`。
源码包包含与二进制对应的修改后上游源码、依赖清单及构建指令；与 GPL-3.0 许可一起分发。
原核心另保存在 `build_tmp/mihomo-custom/mihomo-before-<摘要>.exe`，不随应用分发。

正常打包只校验已构建产物和补丁指纹，不下载上游、不自动更新核心版本。
修改补丁或更新上游后必须先重建；上下文变化或摘要不匹配时终止。

## 兼容边界

- TCP 逐个实际连接候选 IP 申请租约；失败、竞速落败、取消与关闭释放。
- UDP 首次目标及复用 PacketConn 的新目标在发送前申请租约，单 PacketConn 最多 128 个目标。
- 受管出口关闭 MPTCP，防止未管理的附加子流；保持原有 TFO 和接口绑定。
- 请求限定本机 Named Pipe，2 秒期限、32 个并发槽；释放交由有界 worker，最多重试 3 次。
  释放最终失败时保留服务租约到该核心退出，优先避免删除仍可能在用的路由，容量满明确失败。
- 路由服务返回实际接口：VPN 未连接时物理直连，已连接时仍准备 VPN 路由。IPC 或目标连接错误不触发回退。
- TCP 与 UDP socket 绑定该实际接口；已有连接不迁移，UDP 新目标需要切换接口时关闭旧关联。
- HTTP `/version` 返回 `cxvpn-vpn-routes: 2`；Python 在 active VPN 就绪门控中强制检查。
- 源码补丁适用于 Windows；其他系统启用该能力明确失败。ICMP 不包含在本次 TCP/UDP 保证内。
