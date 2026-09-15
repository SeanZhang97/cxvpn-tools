# office 超星 VPN 路由修复

- 日期：2026-09-14
- 分支：main
- 功能：TUN 模式下通过孙河机房 ikev2 访问 office.chaoxing.com。

## 根因

用户的 `chaoxing.com` 后缀规则已生效，Mihomo 正确选择绑定孙河 VPN 接口的 `VPN-1`。
Windows VPN 使用分流模式，既有公网路由仅覆盖 `45.113.20.0/24`。
office 当前真实 DNS 返回另一个地址池，缺少对应 VPN 路由，导致连接日志报网络不可达，
浏览器显示连接关闭。

## 实际调整与影响

- 在当前用户的“孙河机房ikev2”Windows VPN 配置中新增 6 条 RouteMetric=1 的持久路由：
  `140.210.72.160/32`、`140.210.72.162/32`、`140.210.72.164/32`、
  `140.210.72.166/32`、`140.210.72.168/32`、`140.210.72.170/32`。
- 短暂断开并使用已有凭据重连该 VPN，使新增路由载入活动接口。
- 保留原有路由、SplitTunneling、TUN 与 Mihomo 域名规则及其他代理出口配置。
- 未修改业务源码、用户主库和兼容 JSON，未打包、未重启 CXVPNTools。
- 涉及文档：本记录、当月索引、`knowledge/modules/Windows统一域名分流.md`。

## 验证

- 从真实桌面数据视图只读读取主库，核对当前 TUN 配置及超星规则；Controller 回读一致。
- Controller DNS 与公共 DoH 均确认目标池为上述 6 个真实地址。
- 变更后 VPN 已连接，6 条持久路由均在活动接口 48 中存在。
- 无显式 HTTP 代理的 HTTPS 根路径请求从 TLS 握手失败恢复为 HTTP 301。
- Mihomo 日志确认 office 请求命中 `DomainSuffix(chaoxing.com)` 并使用 `VPN-1`。
- 完整用户链接在 Codex 内置浏览器中打开，已显示表单正文与数据日志；未修改表单。
- 未操作原 Edge 标签页，用户可直接刷新；未执行无关代码回归或外部全量测试。

## 注意事项与恢复

- DNS 地址可能变化；这 6 条路由仅覆盖当前目标池，不保证整个 chaoxing.com 的全部子域名。
- 路由作用于目标 IP；访问同 IP 的其他服务也会受 Windows 路由影响。
- 需要撤销时，仅对上述新增前缀执行
  `Remove-VpnConnectionRoute -ConnectionName '孙河机房ikev2' -DestinationPrefix '<具体前缀>'`，
  然后按实际活动路由状态重连 VPN；保留原有 `45.113.20.0/24`。
- 不保存原表单签名 URL、表单数据或 VPN 凭据。
