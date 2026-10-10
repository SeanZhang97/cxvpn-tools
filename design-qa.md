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

---

# 2026-09-18 2FA 账号管理

## Scope and target

- 目标：用户确认的三列卡片稿 `C:/Users/29511/AppData/Local/Temp/codex-clipboard-3fd1f206-cdb1-4421-a1dd-7d4612da0567.png`。
- 实现：现有 `ui/index.html`、`two_factor.css`、`two_factor.js`，复用软件导航、字体和深色背景。没有分组、备注、收藏。
- 方法：IAB 运行真实页面与本地 2FA 桥接；数据是临时库中的虚构账号，系统文件选择器替换为测试路径。没有访问真实平台账号。

## Comparison evidence

- 主视口：1581 × 965 CSS px；确认稿去除顶部 30px Windows 标题栏后对比。相同六个平台/邮箱、默认列表状态；验证码与秒数随时间变化，示例共用公开 RFC 密钥。
- 实际截图：[implementation.png](build_tmp/2fa-preview/implementation.png)
- 完整并排对比，左为确认稿，右为实现：[comparison-full.png](build_tmp/2fa-preview/comparison-full.png)
- GitHub 卡片局部并排对比：[comparison-card.png](build_tmp/2fa-preview/comparison-card.png)
- 较小视口 1100 × 720、两列与实际滚动：[narrow.png](build_tmp/2fa-preview/narrow.png)

## Findings and fixes

- 页面保持三列网格、平台首字母、账号、大号等宽验证码、青色进度条、倒计时、复制与更多菜单的层级。保留真实软件现有导航宽度、字体栈和组件密度。
- 初次比较后增大账号字号与主操作按钮宽度，备份按钮改为描边背景；验证码及卡片留白重新调整。
- [P2，已修复] 通用 `.field` 的 170px 宽度使新弹窗输入框过窄。新增/备份表单现为全宽，高级设置按网格分配，1100 × 720 下复核通过。
- [P2，已修复] 到期刷新曾重建卡片、关闭菜单并丢失焦点。现在仅元数据变化时重建；验证码刷新只更新数值和进度。
- 1100 × 720 下自动转两列，没有页面横向溢出；导航与账号列表有内容时各自滚动，轨道和滑块为全局深色样式；主视口六条数据无多余列表滚动。
- 完整及局部截图复核后，本次范围无未解决的 P0/P1/P2 问题。

## Primary interactions verified

- 平台搜索、无结果状态、一键复制（剪贴板为不含空格的 6 位测试号码）。
- 二维码图片识别、中文组合字符平台、8 位验证码添加；自定义位数菜单用 Space、End、Enter 操作成功。
- 编辑密钥为空，保存后原密钥仍有效；页面离开后 DOM 号码清空，返回后重新读取。
- 加密备份导出显示临时目录内实际完成路径；恢复预览列出 7 个重复账号，确认后导入 0 条，原账号保留。
- 最终页面控制台 warning/error：0。UI 文件与打包资源逐项一致，EXE 的独立二维码解码入口通过。

## Boundaries

- 浏览器验证未操作真实 Windows 文件选择窗口或真实平台绑定；文件选择 API 使用项目已有 pywebview 模式，取消/错误路径有离线测试。
- 用户真实主库未写入测试账号；原有配置仅自动更新检查时间戳发生运行态变化。
- 打包程序已启动且进程响应正常；此结论不等同于真实平台登录或全量端到端回归。

final result: passed

---

## 2026-10-10：2FA 方案 3、顶部保留与手工排序

### Source and state

- 内容区视觉依据：`C:/Users/29511/.codex/generated_images/01a0b3d5-6de5-7471-a17d-5a9d09457ae3/exec-b834264c-853a-45a9-ad9e-74a38c4d842d.png`，1490 × 1056 px；只采用其卡片布局和深蓝主题。
- 顶部依据：用户最后确认的 `C:/Users/29511/AppData/Local/Temp/codex-clipboard-0ad67ffa-3d31-46c0-86a9-e54adcb43dcc.png`，1185 × 221 px。标题、锁图标、说明、下划线、搜索及两个主操作沿用当前源码；生成图顶部不作为实现目标。
- 浏览器实现：`http://127.0.0.1:8877/`，当前生产 UI 文件配合隔离临时库；8 个公开示例账号，深色主题、初始排列。没有访问真实平台或写入真实账号做测试。
- 完整实现截图：`build_tmp/2fa-sort-preview/desktop.jpg`，1482 × 906 px，CSS viewport 1482 × 906，devicePixelRatio=1；截取卡片区为 x=252/y=216/w=1184/h≈669。
- 视觉源卡片区 x=44/y=253/w=1404/h=769 统一缩放至 1184 × 649；横向同尺度比较，记录生成图与实际组件约 3% 的纵向节奏差异。顶部保留造成的首屏起始高度变化为明确需求，不作为偏差。
- 顶部实现截图：`build_tmp/2fa-sort-preview/header-full.jpg`，1424 × 906 px、同等 CSS 视口及 1× 密度；裁取 x=217/y=0/w=1185/h=218。用户图裁去顶部 3 px 外框后同尺寸比较。

### Comparison evidence and iterations

- 首轮完整合成对照：`build_tmp/2fa-sort-preview/comparison-first.png`；首卡局部：`build_tmp/2fa-sort-preview/comparison-first-card.png`。实现原始截图 `first.jpg`，已置于同一比较图中判断。
- [P2，已修复] 平台/账号字号偏小、官方图标和手柄偏细，使身份与拖动入口弱于方案 3。平台 16→18 px、账号 13→14 px、图标 44→48 px、手柄图标 19→24 px；复制按钮 12→13 px 并增加点击尺寸，验证码上限 40→42 px，调整卡片内边距和行距。
- 复核完整合成：`build_tmp/2fa-sort-preview/comparison-final.png`；首卡局部：`build_tmp/2fa-sort-preview/comparison-final-card.png`；对应重拍截图 `desktop.jpg`。身份、手柄、验证码及底部动作层级恢复，无未解决的 P0/P1/P2。
- 顶部合成对照：`build_tmp/2fa-sort-preview/header-comparison.png`。正确规范裁切后，标题/工具条尺度和位置一致；源码比对也确认 header HTML 及其 CSS 与既有版本一致。第一次截图工具的 clip 原点未生效，随后改用完整截图再裁切；未将工具裁切问题误判为布局问题。

### Required fidelity surfaces

- 字体：沿用项目 Segoe UI Variable/Segoe UI/Microsoft YaHei，验证码 Cascadia Code/Consolas 等宽字体。18/14/42 px 层级明确；长名称和账号省略并保留 title；六/八位码不折行，Windows 字体字形与生成图的差异为可接受的系统字体差异。
- 间距：三列同宽、16 px 卡片间距、20/20/18 px 内边距、12 px 圆角；顶栏完全保留。源图各行存在轻微不等高，实际网格统一对齐；3% 高度差不改变列数或隐藏操作，视为可接受偏差。
- 配色：背景沿用 `--bg`，卡片采用深蓝渐变与低对比边界，验证码近白、账号蓝灰，青色进度和拖动状态复用主题色，顶部紫蓝按钮保持不变；hover、focus、disabled 和错误语义清楚。
- 资产：GitHub/GitLab/OpenAI SVG、Google 64 px 官方 PNG、Lucide 手柄均为本地资源；没有手绘品牌替代。GitHub 使用当前官方 Invertocat 白版，取代生成图中的猫形近似；GitLab/OpenAI 裁去透明视口留白，路径与颜色保留。
- 文案：沿用现有业务文案，不新增分组、备注、收藏或排序宣传区；保存信息仅由必要错误反馈及读屏状态提供。示例验证码的秒数随实际计时变化，源图固定 21 秒不作为静态数值约束。

### Primary interactions and responsive evidence

- 鼠标手柄将首条拖到第三条后，后台保存成功；刷新后保持新排列，见 `after-drag.jpg`。通过键盘恢复初始排列。
- Space/Enter 选中、Home/方向键移动、Enter 保存与焦点恢复已在浏览器验证；Escape/Tab 取消、无效落点取消由控制器离线测试覆盖。
- 搜索 GitHub 后拖动两条匹配账号，清空搜索显示其他 6 条位置未变；之后恢复原排列。
- `?fail-order=1` 模拟保存失败，页面恢复后端顺序，见 `save-failure.jpg`；事务失败及过期请求的持久化回滚由 Python 测试覆盖。
- 1000 × 800 的 50 条列表为两列、760 × 800 为一列；分别见 `stress-two-columns.jpg`、`stress-one-column.jpg`。覆盖长 Unicode 名称、8 位码、未知平台首字、单条密钥错误及禁用复制；卡片和底部动作横向溢出数为 0，官方图标加载成功。
- 高密度主区实测全局 WebKit 滑块 `rgba(129,140,248,.34)`、轨道 `rgba(3,7,18,.52)`，无局部冲突颜色；静态检查包含标准 scrollbar-color 兼容路径。空列表无多余滚动，见 `empty.jpg`；未新增横向滚动区域。
- 浏览器 console warning/error：0。顶部及页面原有高级设置使用既有自定义下拉，不新增 select。

### Build and boundaries

- 最小相关检查：Python 56 项通过；JavaScript 排序/品牌、2FA 静态、Google 导入、自定义下拉与 UI 样式检查通过，语法检查通过。
- `uv run --with pyinstaller python build.py` 成功；产物 `dist/CXVPNTools/CXVPNTools.exe` 时间戳 2026-10-10 11:50:14，自动启动 PID 52872，进程响应正常。
- 真实桌面数据视图 `C:/Users/29511/AppData/Local/CXVPNTools` 构建前后配置、全部加密账号及既有顺序保持一致；schema 4→5，仅新增排序表。备份 `recovery-backups/state-before-build-20261010-114951.sqlite3`。
- 打包 UI/脚本/图标/许可证与当前源码逐项相同，业务模块包含于 PYZ，产物不携带 state.sqlite3 或 config.json；打包 EXE 的普通/Google QR 自动识别使用公开合成向量通过。
- 浏览器验证使用隔离桥接；未执行完整端到端回归、真实平台登录、原生 WebView2 指针交互或 Windows 文件选择器。真实库只由正常新版启动执行 schema 升级，没有测试账号、排序或密钥写入。
- 低 schema 旧程序不能直接读取 v5 主库，回退必须先处理备份与兼容性。

### Implementation checklist

- 顶部保持、卡片重设计、官方图标、手工排序及失败恢复：完成。
- 源/实现完整及局部对比，相关离线验证、知识和变更记录、打包与配置核对：完成。
- 残留 P3：生成图品牌标记和 Windows 字体外形的少量差异采用真实资源/项目字体；无需进一步重绘。

final result: passed
