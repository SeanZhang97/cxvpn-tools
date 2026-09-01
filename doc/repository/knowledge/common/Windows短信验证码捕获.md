# Windows 短信验证码捕获（Phone Link / IMAP）

模块: 通用能力 | 代码路径: `core/sms_receiver.py` | 入口: `Catcher(settings, log)`
最后验证: 2026-08-29 | 分支: 非 Git 工作区

## 当前事实

- 每次授权流程按 `config.sms.method` 只启用一个自动接收源：`phone_link` 或 `email`；
  浏览器和工具弹窗的手动输入始终并行保留。
- Phone Link 数据源: `%LOCALAPPDATA%\Microsoft\Windows\Notifications\wpndatabase.db`（SQLite）。
  "手机连接"(Phone Link) 将手机短信以 toast 形式写入该库。
- 打开方式: `sqlite3.connect(f'file:{db}?mode=ro', uri=True)`。
  易错点: 追加 `nolock=1` 反而报 unable to open，必须仅 `mode=ro`。
- 短信 handler 模式: `Microsoft.YourPhone_8wekyb3d8bbwe!YourPhoneMessages_<deviceid>`；
  应用镜像通知为 `YourPhoneNotifications_<安卓包名>`。
- 增量轮询: join `Notification` 与 `NotificationHandler`，条件 `ArrivalTime > since_ft`
  或 `Id > 上次最大Id`，间隔 1~2 秒。
- Payload 为 UTF-8 字节的 toast XML，取全部 `<text>` 节点拼接即短信全文。
- ArrivalTime 为 FILETIME（1601-01-01 UTC 起 100ns），转本地时间需加时区。
- 验证码正则: `(?:验证码|校验码|动态码|确认码|短信码|一次性代码|verification\s*code|code)[^0-9]{0,6}(\d{4,8})`，
  覆盖"验证码为: X""一次性代码: X""code is X"等格式。
- IMAP 数据源使用 `IMAP4_SSL`，只读打开配置目录并以 `BODY.PEEK[]` 获取邮件，
  不依赖 `UNSEEN`、不改变邮件已读状态。启动成功后以当前最大 UID 建立基线，只处理
  本次授权流程之后到达的新 UID；连接初始化超时时以邮件 `Date` 过滤启动前旧邮件。
- 邮件过滤支持固定主题和可选发件人，MIME 正文优先 `text/plain`，没有纯文本时再解析
  `text/html`。iPhone 快捷指令推荐固定主题“超星验证码”，正文直接使用“快捷指令输入”。
- `test_imap` 只验证 SSL、登录和只读打开邮箱目录，不读取邮件正文。163 服务商在认证
  阶段发送 IMAP `ID` 客户端声明。

## 业务规则与边界

- 通知中心只保留"活动"通知，被清除或过期即删除，无法回溯历史 → 必须实时常驻轮询。
- Phone Link 前提: 手机与 Windows 经 Phone Link 配对并开启通知同步；
  验证方法: 手机收短信时 Windows 右下角弹出同款通知。
- 邮箱前提: 邮箱开启 IMAP 并使用授权码或应用专用密码；快捷指令选择立即运行、关闭
  “显示撰写表单”，邮件主题必须与软件配置匹配。
- Android 可由 `pppscn/SmsForwarder` 作为相同邮箱通道的生产者：按短信内容或发件人
  过滤，使用 SMTP SSL 发送固定主题和短信正文；电脑端仍复用 `email` 接收源，无需新增
  协议。Android 侧需按厂商设置自启动、后台运行和忽略电池优化。
- 邮箱授权码随便携 `config.json` 保存在本机，分发前必须清除；推荐使用专用邮箱。
- 控制台中文输出需 `sys.stdout.reconfigure(encoding='utf-8')`。
