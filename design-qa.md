# 网络代理与域名分流 Design QA

## 历史记录：网络代理与域名分流全链路

- 审查日期：2026-08-31
- 审查对象：最终打包并启动的 `dist/CXVPNTools/CXVPNTools.exe`
- 窗口：1426 × 893 px，Windows 桌面深色主题
- 运行状态：代理与统一分流未启用；1 个启用订阅；72 条本地缓存记录；未执行真实 TUN、UAC 或外部订阅请求
- 最终网络代理首页：`build_tmp/design-audit-2026-08-31/final-proxy-home-ready.png`
- 最终域名分流概览：`build_tmp/design-audit-2026-08-31/final-routing-overview.png`
- 最终分流规则：`build_tmp/design-audit-2026-08-31/final-routing-rules.png`
- 最终订阅管理：`build_tmp/design-audit-2026-08-31/final-routing-providers-ready.png`
- 最终节点工作台：`build_tmp/design-audit-2026-08-31/final-node-workbench-empty.png`

### Findings

- 最终截图未发现需要继续修复的 P0、P1 或 P2 视觉、交互或可访问性问题。
- 网络代理首页在配置读取期间明确禁用主操作，加载完成后“开启代理”恢复可用；服务状态、配置状态、进程状态与节点检测不再使用伪实时占位。
- “72”在当前数据条件下明确标注为“缓存记录”，没有再把订阅元数据或未加载缓存冒充为可测速节点。
- 域名分流导航收敛为三个真实 ARIA 选项卡，并把节点工作台作为独立动作；从订阅管理进入节点页后可以按来源返回。
- 规则、订阅和节点空状态均给出下一步动作；主操作、危险操作、状态色和说明文字层级一致。

### Flow health

1. 网络代理首页：Healthy。状态模型覆盖未启用、运行中、配置已启用但服务异常、服务状态未知；主按钮随状态显示开启、关闭、重试或重新检查。
2. 域名分流概览：Healthy。启用开关、接口、DNS、代理策略、配置检查和保存操作在同一任务区内，摘要数据与实际配置一致。
3. 分流规则：Healthy。默认出口、域名命中测试、有序规则和规则启用状态职责清晰；空列表仍能理解当前流量行为。
4. 订阅管理：Healthy。获取/预览、运行服务更新、编辑、启停和删除分层明确；新增订阅默认为未验证且未启用草稿。
5. 节点工作台：Healthy。缓存记录、可用节点、搜索、订阅组筛选、测速与手动选点语义分离；无运行节点时展示可恢复空状态。

### Accessibility and visual QA

- 选项卡使用 `tablist` / `tab` / `tabpanel` 语义并支持方向键、Home、End。
- 规则/全局模式使用 radiogroup 语义和完整键盘切换；自定义下拉支持焦点、选中状态和禁用同步。
- 操作反馈和节点反馈使用可感知状态区域，成功、警告、失败样式与代码类名一致。
- 深色背景下标题、正文、辅助说明和禁用状态对比层级清楚；未发现裁切、重叠、横向溢出或错误圆角。
- 品牌视觉继续复用项目现有 logo、色彩、字体和卡片体系，没有引入不一致的视觉资产。

### Verification boundary

- 已用最终 exe 逐页检查首页、概览、规则、订阅和节点工作台，并保存、重新打开精确截图进行视觉核验。
- 已核验关键控件的可访问性树：加载完成后的“开启代理”可操作，导航、选项卡、开关和返回按钮具有正确角色和状态。
- 为避免修改系统路由、触发 UAC 或访问真实外部服务，本轮没有执行开启 TUN、安装/停止 Windows 服务、真实订阅拉取和真实节点测速；这些属于上线前维护窗口中的集成验证范围。

final result: passed

---

## 当前记录：代理状态超星流光图标

- source visual truth: external generated reference (not committed)
- implementation screenshot path: `design-qa-line-star.png`
- flow-state screenshot path: `design-qa-line-flow.png`
- full-view comparison evidence: `design-qa-line-comparison-full.png`
- focused-region comparison evidence: `design-qa-line-comparison-focus.png`
- viewport: 705 × 270 CSS px
- source pixels: 2028 × 776，按相同长宽比归一化为 705 × 270
- implementation pixels: 705 × 270，浏览器 viewport 705 × 270，截图密度 1:1
- state: 网络代理运行；分别在约 1.9 秒底部流光阶段和约 4.0 秒星星点亮阶段取证

## Findings

- 无 P0/P1/P2 遗留问题。
- 字体与排版：沿用现有 `Segoe UI Variable` / `Microsoft YaHei` 字体栈、字号和信息层级；未为图标优化改写卡片文案或强化营销性标题。
- 间距与布局：保留卡片、文案和按钮的既有布局。完整圆环在 705px 窄视口使用 134px 容器，超星线框稳定居中且全部位于圆环内部，没有挤压或遮挡文案。
- 色彩与视觉变量：使用现有冷蓝紫体系；圆环基础线保持低亮，运行高光、底部能量、星星点亮和外圈脉冲分层明确，没有大面积纯白背景。
- 图像质量与资产一致性：品牌核心来自用户提供的原始 Logo 和最终确认效果稿，并拆成真实透明 PNG 线框、路径与星星图层；完整双轨网络圆环也是独立 PNG 资产，没有用 CSS 图形或手绘 SVG 替代可见资产。
- 文案内容：保持现有状态标题、详情和按钮文案，不引入与本次改动无关的说明或标签。

## Comparison History

### 第一次比较

- [P2] 首次落地沿用宽色带轮廓，仍有白色块状感；外围是分散的开放弧，未形成用户要求的完整动态包裹关系。
- 修复：生成完整闭合的双轨网络圆环资产，并把确认稿中的超星主体提取为单线透明图层；圆环基础层、顺时针层、反向层和脉冲层分别驱动。

### 第二次比较

- [P2] 圆环透明留白过多导致实显直径偏小；扫描层使用硬裁切时出现水平亮边。
- 修复：按有效像素自动裁切圆环并重新适配 512px 画布；扫描层改为柔和遮罩移动，底部能量到星星之间不再出现矩形截断。

### 第三次比较

- 后续证据：`design-qa-line-comparison-full.png` 显示实现保留既有卡片密度，同时达到完整包裹关系；`design-qa-line-comparison-focus.png` 显示线框 Logo、双层圆环、节点与确认稿的结构和视觉层级一致。
- 结果：未发现新的 P0/P1/P2 问题。

## Primary Interactions Tested

- 运行状态类名驱动动画启用，静态测试覆盖 `is-active` 与后端 `running` 状态绑定。
- 浏览器检查圆环高光位置变化、底部流光阶段、星星延迟点亮和脉冲阶段。
- 浏览器控制台 warning/error：0；主要按钮在预览中可见且无遮挡。
- `prefers-reduced-motion` 降级规则由现有 UI 静态测试覆盖。

## Follow-up Polish

- P3：高 DPI 真机上可根据实际 WebView2 抗锯齿观感微调细线基础透明度，但不影响当前交付。

final result: passed
