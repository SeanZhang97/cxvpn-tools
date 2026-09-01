# Windows VPN 凭据与 RAS API

模块: 通用能力 | 代码路径: `core/ras_cred.py`、`core/eap_connect.py`、`core/vpn_connect.py`、`core/vpn_service.py`、`core/vpn_os.py`
最后验证: 2026-08-29 | 分支: 非 Git 工作区

## 当前事实（Win11 25H2 实测）

### rasdial 与状态行为

- 本机 `rasdial` 没有保存凭据开关；`/savecredentials`、`/SAVECRED` 都会打印
  USAGE，且退出码仍可能为 0，因此不能只依赖进程退出码判定成功。
- 失败时退出码通常等于 RAS 错误码，例如 628、691、703、809。
- `RasDialW` 即使返回非零错误，也可能在 `lphRasConn` 写入非空连接句柄。每个非空句柄
  最终都必须与 `RasHangUpW` 配对；失败后调用 `RasHangUpW` 并等待约 3 秒，让 RAS
  状态机释放端口，否则下一次拨号可能持续返回 756。成功连接的句柄不能在连接函数
  返回前释放。
- notifier 为空的 `RasDialW` 是同步调用，PPP 协商停滞时可能长时间不返回。本项目普通
  凭据路径固定使用 `RasDialFunc1` 异步回调；90 秒仍无成功或错误终态时，以本次
  `HRASCONN` 调用 `RasHangUpW`，返回 1460 并按网络错误退避。
- 无参 `rasdial` 的已连接列表既可能把标题和条目放在同一行，也可能先输出
  `Connected to` / `已连接到` 标题、再逐行输出连接名称。解析时必须同时兼容，
  并按完整名称 `casefold` 精确匹配。
- `Get-VpnConnection.ConnectionStatus` 在刚连接后可能短暂滞后。应用状态应合并
  `Get-VpnConnection` 配置列表与 `rasdial` 实时会话，连接和断开后强制刷新。
- 断开指定条目使用 `rasdial <名称> /disconnect`。

### 普通 RAS 凭据

- `RasSetCredentialsW` 以 `UserName|Password` 写入普通 RAS 凭据槽；写入成功后用
  `RasGetCredentialsW` 回读用户名和密码存在标记。部分条目需用
  `RasSetEntryDialParamsW` / `RasGetEntryDialParamsW` 作为兼容写入和确认路径。
- Windows 不向应用返回保存密码的明文；`RasGetCredentialsW` 的密码字段通常表现为
  星号。软件需要回显或直接拨号时，以自身 `config.json creds` 中的账号密码为来源。
- 微软文档允许把 `RasGetCredentialsW` 返回的密码句柄交给 `RasDialW`，但本机实测该
  包装路径发起 PPTP 时，RasClient 事件中的拨号用户变成 `*\\` 并报 628；同一条目
  从 Windows 设置页用真实用户名可成功。因此本项目不再把星号/密码句柄复制到
  `RASDIALPARAMS`，而是把软件保存的真实用户名和密码直接交给 `RasDialW`。
- 软件未保存完整账号密码时，不尝试依赖 Windows 设置页的私有连接状态，也不调用
  会显示界面的 API；在拨号前返回 `needs_credentials`，由软件凭据弹窗补录。若弹框由
  一次连接动作触发，保存成功后 UI 自动续接原目标；主动打开凭据弹框时只保存不拨号。

### EAP-MSCHAPv2 凭据与无界面连接

- IKEv2 的 EAP-MSCHAPv2 不是普通用户名/密码拨号路径。直接把账号密码放入普通
  `RASDIALPARAMS` 可能返回 703；这不表示账号密码错误。
- 保存凭据时从 `Get-VpnConnection.EapConfigXmlStream` 读取 EAP 配置，并调用
  `EapHostPeerConfigXml2Blob` 转换配置。当前项目支持 EAP type 26
  （EAP-MSCHAPv2）。
- 账号密码按 Microsoft EAP Host 用户凭据 XML 构造，经
  `EapHostPeerCredentialsXml2Blob` 转换，再由 `RasSetEapUserData` 写入当前用户的
  该 VPN 条目。
- 写入后必须调用 `RasGetEapUserIdentity`，并设置 `RASEAPF_NonInteractive`。成功
  说明 Windows 能在不显示 EAP 用户界面的情况下取得身份数据；拨号时把返回的
  `RASEAPINFO` 交给 `RasDialW`。
- `RasDialDlgW` 的职责就是显示系统拨号对话框，不属于无界面连接方案。本项目连接
  路径不得调用它；`rasphone.pbk` 的 `PreviewUserPw`、`PreviewDomain`、
  `ShowDialingProgress` 和 `SkipDoubleDialDialog` 只能减少经典预览/进度窗口，不能
  替代 EAP 非交互凭据准备。
- `rasphone.pbk` 可能是 UTF-16、带 BOM UTF-8、无 BOM UTF-8 或旧 ANSI。修改目标
  条目时必须识别并保持原编码，避免中文条目名损坏。

### 保存、连接和授权边界

1. 软件保存账号密码时立即写入自身配置、普通 RAS 凭据槽以及适用的 EAP 用户数据；
   保存动作不拨号、不打开网页、不触发远端授权。
2. 保存成功只说明本机凭据已准备好。远端页面是否授权只在用户主动或既有定时授权
   流程中处理，不再维护 `pending_authorization`、`validated`、
   `validation_failed` 等连接前校验状态。
3. 普通 VPN 连接使用软件保存的真实账号密码调用 `RasDialW`。返回 703 时进入 EAP
   type 26 的非交互准备和 `RASEAPINFO` 拨号；整个过程不显示 Windows 连接弹框。
4. 普通连接返回 691 时仍按现有业务语义视为密码错误或远端授权到期，并触发既有续期
   恢复流程；是否授权不作为保存凭据的前置验证。
5. 应用启动时对每条完整工具凭据重新执行普通 RAS 和 EAP 同步，以修复旧版本漏写，
   不发起拨号或远端授权。
6. worker 的自动连接检查和拨号运行在独立守护线程；主循环不得等待该线程持有的 VPN
   操作锁，以保证浏览器请求、授权任务与 RAS 事件仍能被领取。

### 连接错误分类与恢复策略

- `credentials`：软件未保存完整账号密码，暂停自动连接并提示在软件中补录。
- `authentication`：691 可能是密码错误或远端授权到期，按既有续期策略处理。
- 本机 PPTP 与 IKEv2 实测存在 `RasDialW` 表层返回 628、但同一次拨号的
  `Microsoft-Windows-EapMethods-RasChap/Operational` 事件 101 以 `int1=691`
  记录真实认证失败的情况。仅在事件时间晚于本次拨号开始、且 `Domain` 或 `Username`
  与本次账号匹配时，才将 628 提升为 691；无匹配事件仍保留网络错误语义。
- `busy`：756 表示 VPN 正在拨号，不是授权失败；后台 30 秒后再试，不触发授权。
- `network`：1460 表示应用硬超时已终止本次拨号，按其它临时网络错误退避重试。
- `network`：619、628、629、668、718、721、792、800、807、809、829、868 等按
  连续失败次数退避。
- `service`：633、692、711 表示本机端口、设备或 RAS 服务异常，建议修复 VPN 服务。
- `profile`：621～625 表示电话簿或条目问题，等待用户修正或重建。
- `configuration`：703、720、734、781、786～791、793、798、806、812 和 IKE
  138xx 等固定配置问题，暂停自动重试。
- 手动点击连接不受后台暂停限制；连接成功、配置或凭据更新、续期或服务修复成功时
  清除后台失败状态。

## 诊断手段

- RasClient 事件：20221 开始拨号、20223 链路建立、20225 认证成功、20226 终止、
  20227 失败。20221/20225 的 `Dial-in User` 可用于确认软件实际提交了哪个用户名。
- EAP-MSCHAPv2 认证事件位于
  `Microsoft-Windows-EapMethods-RasChap/Operational`；事件 101 的 XML
  `EventData/int1` 是数值错误码。Windows PowerShell 子进程可能因继承宿主
  `PSModulePath` 而无法自动载入 `Get-WinEvent`，调用前应按系统绝对路径显式导入
  `Microsoft.PowerShell.Diagnostics.psd1`。
- 当前连接：无参 `rasdial`；配置和枚举状态：`Get-VpnConnection`。
- Windows `VpnClient` 模块没有 `Connect-VpnConnection` cmdlet；连接使用 Win32 RAS
  API，不能把不存在的 PowerShell 命令当作连接路径。
