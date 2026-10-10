# 2FA 账号管理

- 模块：本地 TOTP 验证器
- 关键词：2FA、TOTP、DPAPI、二维码、加密备份、cx2fa、Google Authenticator、otpauth-migration、拖拽排序、官方图标
- 代码路径：`core/two_factor.py`、`core/two_factor_api.py`、`core/two_factor_backup.py`、`core/two_factor_qr.py`、`core/two_factor_migration.py`、`core/state_store.py`、`api.py`、`main.py`、`ui/two_factor.js`、`ui/two_factor_order.js`、`ui/two_factor_brands.js`、`ui/two_factor.css`、`ui/icons/brands/`、`ui/index.html`
- 入口：导航 `two-factor`、`Api(TwoFactorApi)`、`main.py --otp-decode`
- 最后验证：2026-10-10 | 分支：main（工作区）

## 页面与桥接

- 工作区为账号卡片，支持平台/账号搜索、添加、编辑、删除、复制、备份与恢复；不包含分组、备注或收藏。
- `get_otp_accounts` 只返回元数据、当前验证码、到期时间或单条错误；密钥不进入 CFG、localStorage、普通配置导出或全局状态流。
- 页面只在进入、聚焦和验证码到期时向后端取码；倒计时使用服务端时间与本地单调时钟，系统时间跳变时重新取码。页面隐藏即清空号码、停止计时；旧请求通过 epoch 丢弃。
- 自动换码不重建账号卡片，保留键盘焦点和菜单；复制前通过 `get_otp_code` 取当前号码，不复制旧 DOM 内容。
- 编辑不回显现有密钥；空白表示保留，输入新密钥则替换。关闭弹窗清空密钥和备份密码。
- 高级算法/位数选择复用 `RoutingWorkspace.enhanceSelect`；滚动区域沿用全局深色滚动条。
- 标题、搜索框、添加和备份操作保持原有样式，内容为响应式三列卡片，小窗口切换为两列或一列。手柄为独立可拖动按钮，复制、选中文字及更多菜单不会触发排序。
- 手柄支持鼠标拖动及键盘操作：Space/Enter 选中，方向键/Home/End 移动，Enter/Space 保存，Escape/Tab 取消；提交结束恢复手柄焦点。页面切换或搜索变化取消未提交的移动。
- 筛选后的拖拽只替换完整列表中匹配账号所占的位置，筛选外账号保持原位。保存期间不发起第二次排序；超时上限 10 秒，失败恢复显示并重新读取后端顺序。
- GitHub、Google、GitLab、OpenAI/ChatGPT 按平台名称或明确官方域名匹配本地品牌资源。未知平台显示首字；不按任意子串推断品牌、不请求远程 favicon。图标来源见 `ui/icons/brands/README.md`。

## 生成、存储和并发

- PyOTP 生成 RFC 6238 TOTP，支持 SHA1/SHA256/SHA512、6/8 位及整数周期（默认 30 秒）。单账号输入可为 Base32 或标准 `otpauth://totp/` URI；Google 批量迁移码使用独立导入流程，HOTP 暂不支持。
- 主用户库 schema v4 新增 `otp_accounts`，v5 额外新增 `otp_account_order(account_id,position)`，外键删除级联；沿用原主库连接与短事务，不改变代理配置 revision 或导出到 config.json。
- `reorder_otp_accounts(ids, expected_order)` 在一个事务中校验完整 ID 集合和原始顺序，再替换位置记录；账号增删或其他窗口调整顺序后，过期请求拒绝整次写入。排序不修改密文、条目 revision、创建/更新时间，也不使已生成的导入预览失效。
- 有位置记录的账号优先按 position 展示，没有位置记录的旧账号沿用 created_at/id 顺序，新增/导入账号追加末尾；编辑保留位置。加密备份按当前顺序导出，新库恢复沿用文件顺序，已有账号的位置保持原状。
- 重新打包前自动备份当前主库；低 schema 版本不能直接读取新版主库，回退须按主库恢复规则处理，不自动降低版本。
- 密钥及生成参数由当前 Windows 用户的 DPAPI 加密，条目 ID 作为附加熵。平台、账号和非秘密参数用于列表展示，密钥为 BLOB。
- 同平台与账号按 casefold 判重；编辑/删除检查条目 revision，避免旧窗口覆盖新修改。单条不可读密钥不会阻断其他卡片。
- 资源边界为最多 1000 个账号，平台 128 字符、账号 256 字符、密钥最多 256 个 Base32 字符。

## 二维码

- 系统文件选择器或剪贴板图片进入独立子进程，Pillow 校验 PNG/JPEG/WebP，zxing-cpp 识别二维码。“添加/编辑账号”的图片按钮和粘贴入口通过 `--otp-decode --otp-auto` 自动分辨标准 TOTP 或 Google 导出格式。
- 标准 TOTP 返回单个账号参数填入编辑表单；Google 导出码直接创建批次预览，前端转入已有 Google 导入页签，不重新选择或重复解码图片。`import_otp_qr`、`decode_otp_image` 返回 `kind=totp/google`，Google 分支只有预览元数据，不返回生成密钥或 URI。
- 自动转入 Google 预览时保留原编辑表单及条目 revision；关闭或 Escape 返回原表单并聚焦图片按钮，确认导入成功后关闭两层窗口并清空原表单密钥。旧预览取消必须先完成，转入时不得再取消已经生成的新批次。
- 输入最多 6 MB、800 万像素，子进程超时 10 秒并终止；图片与 URI 通过二进制管道传递，不写临时明文文件。
- `--otp-decode` 在桌面运行环境跳转、用户配置、worker 和 GUI 初始化之前分派。无控制台 EXE 通过继承的标准句柄读写管道。

## Google Authenticator 导入

- 入口是“2FA 管理 → 备份与恢复 → Google 导入”，以及添加/编辑账号中的二维码图片或粘贴自动识别。`preview_otp_google_images` 多选 PNG/JPEG/WebP，`preview_otp_google_image` 接收剪贴板图片，`confirm_otp_google_import` 确认保存。
- iPhone 在 Google Authenticator 菜单选择转移账号、导出账号并生成二维码；软件导入二维码中的生成密钥，持续生成 TOTP，不是保存某一时刻的六位号码。
- `--otp-decode --otp-migration` 在隔离子进程识别图片中的全部迁移二维码（每图最多 32 个）。迁移 URI 为 `otpauth-migration://offline?data=...`，Base64 protobuf 使用有界字段读取，不新增 protobuf 核心依赖。
- 接受迁移版本 1 和 2；已用真实 iPhone 导出截图确认版本 2 的账号与分页字段兼容，附加未知字段由有界读取器跳过。更高未知版本仍拒绝，并提示更新本软件。校验 batch_id、batch_size 与 batch_index；支持乱序、分次补页与相同页重复，同页不同内容或不同批次拒绝，操作失败不改变已收集的页。
- 全部页收齐后才生成确认令牌，缺页时返回从 1 开始的缺失页码，确认前不落库。平台/账号解析保留 Unicode；算法 SHA1/SHA256/SHA512、6/8 位，迁移格式没有周期字段，使用 30 秒。缺省枚举按 SHA1、6 位、TOTP 处理。
- HOTP、MD5、未知算法/位数及无效条目以原因列入跳过预览；重复或冲突项保留现有记录。Google 导入不替换不可读的现有密钥，此类恢复只允许已验证密码的 `.cx2fa` 备份。
- 收集的页及待导入条目只在后台保留 DPAPI 密文；返回前端的预览只有平台、账号、计数和跳过原因，没有密钥或迁移 URI。页面切换、关闭和重新开始按顺序取消旧预览，避免迟到取消清空新批次。
- 页收集的闲置期限、确认令牌有效期均为 300 秒，备份与 Google 令牌分类型且单次使用。导入和备份任务互斥；确认时沿用主库条目 revision 签名和原子事务。
- 每次最多 100 张图、合计 64 MB、二维码处理总期限 60 秒，单张识别最多 10 秒；迁移载荷最多 64 KB，批次最多 100 页/1000 个账号。图片或解码后的明文不由软件写入临时文件或日志，也不访问远程解码服务。
- 格式参考：[Aegis google_auth.proto](https://github.com/beemdevelopment/Aegis/blob/master/app/src/main/proto/google_auth.proto)，手机导出步骤参考 [Google iOS 帮助](https://support.google.com/accounts/answer/1066447?hl=zh-Hans&co=GENIE.Platform%3DiOS)。

## 备份与恢复

- `.cx2fa` 使用固定版本头、随机 16 字节盐、12 字节 nonce、scrypt（N=2^17/r=8/p=1）和 AES-256-GCM；版本头与盐参与认证。密码 10–512 字符，不持久化。
- 导出使用系统保存窗口，同目录临时密文文件、fsync、原子替换；成功返回实际路径，取消不报告成功。普通配置备份不包含此账号表。
- 恢复文件上限 4 MB，完整验证后预览新增、重复和冲突；已能读取的同名不同密钥条目跳过，不覆盖。
- 原 Windows 用户密文不可读时，可从已验证密码的备份恢复对应条目；预览明确列出这些恢复项。使用新用户的 DPAPI 重新加密后与新增一起提交。
- 预览令牌单次使用、有效期 300 秒，只在后台保留 DPAPI 密文；确认时核对全部条目 ID/revision 签名。期间账号变化则拒绝并要求重新预览。
- 导出/恢复任务互斥；日志记录提交、领取、执行及终态，不打印密钥、验证码、密码、URI 或第三方异常原文。

## 验证边界

- `python -m unittest tests.test_two_factor` 使用临时主库、真实 DPAPI、RFC 测试向量、加密/篡改/错误密码、二维码独立进程与超时、Unicode 和原子回滚。
- `python -m unittest tests.test_two_factor_migration` 覆盖版本 1/2、未知版本拒绝、附加字段、迁移字段/边界、乱序/重复/缺页/混批、跳过不支持账号、确认前不写库、不可读密钥保留、过期/取消/并发保护、多选与剪贴板的真实二维码子进程。
- `node tests/test_ui_two_factor_import.js` 覆盖取消与读取的先后顺序、缺页禁用确认、收齐后的预览、取消额外选图保留预览、重新开始、粘贴与 Google 确认入口；还覆盖编辑表单识别后转入预览、不取消新批次、关闭后原输入保留、成功后清空，以及普通码仍填表。
- `node tests/test_ui_two_factor.js` 验证页面与桥接结构；路由/样式测试维护自定义下拉框与密码可见性清单。
- `python -m unittest tests.test_two_factor_order` 验证 v4 增量迁移、排序持久化、密文/配置不变、并发冲突、无效请求、事务回滚及增删编辑导入的顺序。
- `node tests/test_ui_two_factor_order.js` 覆盖手柄拖动、键盘保存/取消、筛选外位置保留、无效落点取消及品牌精确匹配；隔离浏览器预览进一步验证鼠标拖动、刷新保留及保存失败恢复。
- 浏览器验证使用隔离临时账号及本地桥接，文件选择器由测试路径代替；没有对真实平台执行登录或绑定。
