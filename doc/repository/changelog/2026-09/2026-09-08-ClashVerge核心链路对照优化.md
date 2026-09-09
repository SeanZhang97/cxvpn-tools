# Clash Verge 核心链路对照优化

- 日期：2026-09-08
- 功能：订阅、节点测速与网络代理可靠性优化
- 分支：main

## 需求与根因

对照 `C:\develop\workspace\clash-verge-rev-reference` 的 Clash Verge Rev v2.5.2，补齐本项目在脏订阅地址、测速响应兼容和系统代理快切确认上的边界处理。

## 涉及文件

- `core/routing.py`
- `core/subscription_store.py`
- `core/routing_speedtest.py`
- `tests/test_routing_schema.py`
- `tests/test_routing_reliability.py`
- `tests/test_routing_speedtest.py`
- `tests/test_proxycore.py`

## 关键设计

- 兼容 `path&token=...` 形式的订阅 URL，仅在没有正式 query 且确实能解析出参数时迁移到 query；正常 URL 不改写。
- 缓存状态读取遇到检查期间文件被清理的竞态时按不可用处理，不阻断订阅回退调度。
- 节点测速兼容 Mihomo 返回的数字字符串和浮点延迟，拒绝 `bool` 伪延迟，并记录单节点耗时；同时兼容 `provider_name` 与 Clash Verge 风格的 `provider` 字段。
- 系统代理快切同时校验原生服务返回的运行模式/代理状态，并对 Windows 注册表异步写入做 1.5 秒有界回读；状态不一致时尽力恢复待机后报错。
- 启动时按持久化启用状态与实际 Windows 代理端点对账：恢复 CXVPN 自身的接管，或只清理精确匹配当前 `mixed-port` 的残留代理；第三方代理端点保持不变。

## 影响范围

不改变现有订阅缓存原子提交、Mihomo 临时解析、测速取消协议、路由事务锁和常驻核心生命周期；仅增强输入兼容、结果归一化及终态确认。

## 验证

已通过 158 项相关 Python 离线测试，覆盖路由 schema、订阅缓存可靠性、测速任务、代理快切、路由选择、原生服务和核心路由逻辑。

## 注意事项

本次未引入订阅流量/到期元数据公共字段；参考实现中的 Merge/Script 注入、链式代理和 profile 任意扩展不纳入本项目固定配置生成边界。
