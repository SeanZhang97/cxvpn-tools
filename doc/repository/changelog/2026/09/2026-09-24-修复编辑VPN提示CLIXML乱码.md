# 2026-09-24 修复编辑VPN提示CLIXML乱码

## 日期

2026-09-24

## 功能名

修复编辑/添加/删除 VPN 后成功提示与结果区显示 `#< CLIXML` 乱码

## 分支

main

## 问题根因

- `core/vpn_os.py` `_ps()` 用 `subprocess` 捕获 PowerShell 的 stdout/stderr。
  Windows PowerShell 5.1 在 stderr 被重定向时，会把进度（progress）、错误等
  非输出流序列化为 CLIXML（`#< CLIXML` 开头）写入 stderr。
- `Set-VpnConnection` 等 CIM cmdlet 每次执行都会发进度记录，成功时 stderr
  也塞满进度 CLIXML；`set_vpn()`/`add_vpn()`/`remove_vpn()` 取
  `message = err or out`，把这段 XML 当结果消息返回前端，Toast 与弹窗
  反馈区原样展示。
- CLIXML 内的中文乱码是序列化时经系统 ANSI 代码页的有损转换，PowerShell
  侧已损坏，无法在 Python 侧还原。

## 修复动作

- `_ps()` 命令前缀增加 `$ProgressPreference = 'SilentlyContinue'`，抑制
  Write-Progress 进度流。
- 新增 `_clean_stderr()`：识别 CLIXML 包装后仅提取 `<S S="Error">` 错误
  文本（还原 `_xHHHH_` 转义与 XML 实体，丢弃进度记录、位置行、脚本回显与
  CategoryInfo 噪声）；纯进度 CLIXML 清洗为空，非 CLIXML 的 stderr 原样透传。
  成功路径消息从此为空，前端按既有逻辑回落到“VPN 配置已更新”等默认文案。
- 修复点在共享 `_ps()`，同时覆盖添加/编辑/删除 VPN、IP 查询、EAP 凭据等
  全部 PowerShell 调用方。

## 涉及文件

- `core/vpn_os.py`
- `tests/test_vpn_ps_output.py`（新增）

## 验证情况

- 本机真实探针复现：默认参数下成功命令 stderr 即 `#< CLIXML` 进度记录；
  新 `_ps()` 下成功路径 err 为空、失败路径返回可读错误文本。
- `python -m unittest tests.test_vpn_ps_output`：7 项全部通过，夹具取自本机
  真实 CLIXML 输出，含中文与非 BMP 字符用例。
- `tests.test_vpn_creation_flow` 有 2 个失败为改动前已存在
  （`Api` 缺少 `routing_network` 属性），与本次改动无关，未处理。
- 未在打包前对真实 VPN 条目做编辑回归；打包后通过实际编辑 VPN 复核。

## 注意事项

- CIM cmdlet 失败消息本身可能携带 Windows 侧已损坏的本地化文本
  （如 VPN 名显示为问号），属系统格式化限制，本次仅保证不再显示 XML 原文。
