# 2FA 账号管理

- 模块：本地 TOTP 验证器
- 关键词：2FA、TOTP、DPAPI、二维码、加密备份、cx2fa
- 代码路径：`core/two_factor.py`、`core/two_factor_api.py`、`core/two_factor_backup.py`、`core/two_factor_qr.py`、`core/state_store.py`、`api.py`、`main.py`、`ui/two_factor.js`、`ui/two_factor.css`、`ui/index.html`
- 入口：导航 `two-factor`、`Api(TwoFactorApi)`、`main.py --otp-decode`
- 最后验证：2026-09-18 | 分支：main（工作区）

## 页面与桥接

- 工作区为账号卡片，支持平台/账号搜索、添加、编辑、删除、复制、备份与恢复；不包含分组、备注或收藏。
- `get_otp_accounts` 只返回元数据、当前验证码、到期时间或单条错误；密钥不进入 CFG、localStorage、普通配置导出或全局状态流。
- 页面只在进入、聚焦和验证码到期时向后端取码；倒计时使用服务端时间与本地单调时钟，系统时间跳变时重新取码。页面隐藏即清空号码、停止计时；旧请求通过 epoch 丢弃。
- 自动换码不重建账号卡片，保留键盘焦点和菜单；复制前通过 `get_otp_code` 取当前号码，不复制旧 DOM 内容。
- 编辑不回显现有密钥；空白表示保留，输入新密钥则替换。关闭弹窗清空密钥和备份密码。
- 高级算法/位数选择复用 `RoutingWorkspace.enhanceSelect`；滚动区域沿用全局深色滚动条。

## 生成、存储和并发

- PyOTP 生成 RFC 6238 TOTP，支持 SHA1/SHA256/SHA512、6/8 位及整数周期（默认 30 秒）。输入可为 Base32 或标准 `otpauth://totp/` URI；HOTP、批量迁移码不支持。
- 主用户库 schema v4 新增 `otp_accounts`，沿用原主库连接与短事务；独立记录 revision，不改变代理配置 revision 或导出到 config.json。
- 密钥及生成参数由当前 Windows 用户的 DPAPI 加密，条目 ID 作为附加熵。平台、账号和非秘密参数用于列表展示，密钥为 BLOB。
- 同平台与账号按 casefold 判重；编辑/删除检查条目 revision，避免旧窗口覆盖新修改。单条不可读密钥不会阻断其他卡片。
- 资源边界为最多 1000 个账号，平台 128 字符、账号 256 字符、密钥最多 256 个 Base32 字符。

## 二维码

- 系统文件选择器或剪贴板图片进入独立子进程，Pillow 校验 PNG/JPEG/WebP，zxing-cpp 识别单个 TOTP 二维码。
- 输入最多 6 MB、800 万像素，子进程超时 10 秒并终止；图片与 URI 通过二进制管道传递，不写临时明文文件。
- `--otp-decode` 在桌面运行环境跳转、用户配置、worker 和 GUI 初始化之前分派。无控制台 EXE 通过继承的标准句柄读写管道。

## 备份与恢复

- `.cx2fa` 使用固定版本头、随机 16 字节盐、12 字节 nonce、scrypt（N=2^17/r=8/p=1）和 AES-256-GCM；版本头与盐参与认证。密码 10–512 字符，不持久化。
- 导出使用系统保存窗口，同目录临时密文文件、fsync、原子替换；成功返回实际路径，取消不报告成功。普通配置备份不包含此账号表。
- 恢复文件上限 4 MB，完整验证后预览新增、重复和冲突；已能读取的同名不同密钥条目跳过，不覆盖。
- 原 Windows 用户密文不可读时，可从已验证密码的备份恢复对应条目；预览明确列出这些恢复项。使用新用户的 DPAPI 重新加密后与新增一起提交。
- 预览令牌单次使用、有效期 300 秒，只在后台保留 DPAPI 密文；确认时核对全部条目 ID/revision 签名。期间账号变化则拒绝并要求重新预览。
- 导出/恢复任务互斥；日志记录提交、领取、执行及终态，不打印密钥、验证码、密码、URI 或第三方异常原文。

## 验证边界

- `python -m unittest tests.test_two_factor` 使用临时主库、真实 DPAPI、RFC 测试向量、加密/篡改/错误密码、二维码独立进程与超时、Unicode 和原子回滚。
- `node tests/test_ui_two_factor.js` 验证页面与桥接结构；路由/样式测试维护自定义下拉框与密码可见性清单。
- 浏览器验证使用隔离临时账号及本地桥接，文件选择器由测试路径代替；没有对真实平台执行登录或绑定。
