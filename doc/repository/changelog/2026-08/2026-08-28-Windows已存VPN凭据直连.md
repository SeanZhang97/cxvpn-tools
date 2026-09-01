# 2026-08-28 Windows 已存 VPN 凭据直连

- 日期: 2026-08-28
- 功能名: Windows 已存 VPN 凭据直连
- 分支: 非 Git 工作区
- 类型: 连接体验优化 / 安全性优化

## 需求概述

新用户已经在 Windows 中配置 VPN 并保存账号密码时，软件不应因为无法取得明文
密码就要求重复输入。首次连接应先复用 Windows 已保存凭据，仅在系统确实没有可用
密码或认证失败时提示补录。

## 问题根因

- Windows RAS 出于安全原因只返回密码句柄，不返回明文；旧说明把“不能读取明文”
  错误等同于“不能用于连接”。
- 无工具凭据路径先调用了系统并不存在的 `Connect-VpnConnection` cmdlet，异常又被
  静默吞掉，连接能力和诊断语义均不可靠。
- 原生 `RasDial` 包装器没有检查凭据读取结果、是否存在已存密码及拨号返回码，UI
  无法区分“系统没有密码”和“已有密码但授权到期”。

## 涉及文件

- `core/vpn_connect.py`
- `core/vpn_service.py`
- `api.py`
- `ui/app.js`
- `ui/index.html`
- `tests/test_vpn_saved_credentials.py`
- `doc/repository/knowledge/common/Windows-VPN凭据与RAS-API.md`
- `doc/repository/knowledge/modules/CXVPN管理器.md`

## 关键设计

- 无工具凭据时，通过隔离的 PowerShell/C# 包装器调用
  `RasGetCredentialsW → RasDialW`。前者返回不可逆密码句柄，后者由 Windows 内部
  使用该句柄连接，软件全程不读取明文密码。
- 移除不存在的 `Connect-VpnConnection` 路径；只有原生包装器被策略阻止、超时或
  初始化失败时才退回裸 `rasdial`。原生拨号已给出认证结果时不重复拨号。
- VPN 名称经 UTF-8 Base64 编码后再传入 PowerShell/C# 包装器，不直接拼接脚本，
  避免名称中的引号或换行改变脚本结构。
- 系统未保存密码且拨号返回 691 时返回 `needs_credentials`，UI 自动打开凭据弹窗；
  已有工具凭据的 691 继续触发授权续期，避免把授权到期误判为首次缺凭据。
- 凭据弹窗和帮助页改为“先直接连接、必要时补录”，检测到 Windows 已存凭据时
  明确提示无需重新输入。
- 顺带修正电话本静默配置读取未关闭文件句柄的问题。
- 后续补充 RAS 错误分类策略：临时网络错误退避重试；凭据、配置、证书、电话簿和
  本机服务类错误暂停自动重试；修改配置、更新凭据、续期或修复成功后解除暂停。
- 扩充 621～625、628、629、633、668、711、718、721、734、781、786～793、
  798、806、807、829 等常见错误提示，并把 692 修正为端口或设备硬件故障。
- Worker 通过 `state.connection_error` 向总览暴露错误码、类别、是否重试和是否建议
  修复；总览直接显示当前失败原因，服务类错误同步提示使用“修复 VPN 服务”。

## 影响范围

- 影响总览页手动连接、开启自动连接后的后台重连，以及 VPN 凭据弹窗提示。
- 不改变 VPN 配置结构、隧道类型、授权续期接口或已有工具凭据的显式拨号行为。
- 已经存在于 `config.json` 的工具凭据仍按原逻辑使用；本次不会迁移或删除用户数据。

## 验证情况

- `py_compile` 通过：`main.py`、`api.py`、`core/*.py` 和相关测试。
- `node --check ui/app.js` 通过。
- VPN 凭据与错误策略测试共 14 项通过，覆盖 Windows 密码句柄成功连接、无系统
  密码、原生认证失败不重复拨号、包装器不可用回退、补录标记、691 续期分流、VPN
  名称安全传参、692 修正、临时网络退避、配置错误暂停和解除暂停；项目 Python
  离线测试共 23 项全部通过。
- 使用不存在的虚拟 VPN 名称验证本机 PowerShell/C# RAS 包装器可正常编译并返回
  `RasGetCredentials` 错误，未连接真实 VPN。
- 未执行真实 VPN 端到端连接，避免访问真实服务和使用真实凭据。
- 执行 `uv run --with pyinstaller python build.py`：早期首次因运行中的
  `CXVPN管理器.exe` 占用旧 `ClrLoader.dll` 失败；按项目规则强制关闭进程后重试
  成功；错误策略优化后再次打包成功。最终产物为
  `dist/CXVPN管理器/CXVPN管理器.exe`，时间戳 `2026-08-28 07:19:41`。
- 打包前后 `dist/CXVPN管理器/config.json` SHA-256 均为
  `6AF741BDFD76EAC131377029E648562300FF7F5D184875C909CAB0E50031151F`，用户配置
  完整保留；打包资源已检出 `needs_credentials`、`state.connection_error`、服务
  修复提示和分类重试帮助文案。

## 注意事项

- 密码句柄由 Windows 管理，禁止依赖其星号内容或尝试还原明文，只能原样交回 RAS。
- 服务端轮换密码后，Windows 保存的旧凭据仍会认证失败；续期或重试后仍失败时，
  用户需要补录新密码。
