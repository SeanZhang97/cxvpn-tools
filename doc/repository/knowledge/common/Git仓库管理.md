# Git 仓库管理

模块：开发与交付 | 关键词：Git, GitHub, gitignore, 敏感配置, 构建产物, 运行时二进制
代码路径：`.gitignore`, `core/config.py`, `build.py`, `build_runtime.py`, `runtime/routing/`
入口：仓库根目录 | 最后验证：2026-09-01 | 分支：`main`

## 跟踪边界

- `config.json` 是应用根目录下的便携式用户配置，可包含手机号、VPN 名称与凭据、
  邮箱授权码、VLM API Key、订阅 URL 和 Controller secret，因此必须被 `.gitignore` 排除。
- `webview_data/` 和 `browser_data*/` 是 WebView2 用户数据目录，可包含 Cookie、
  Local Storage 和会话数据；`captcha_cache/` 与 `routing_data/` 也属于本地运行状态，均不跟踪。
- `dist/`、`build_tmp/`、`routing-service/target/`、Python 虚拟环境与缓存是可重建产物，不进入代码仓库。
- `runtime/routing/mihomo.exe` 和 `WinSW-x64.exe` 是打包直接依赖的锁定第三方运行时，
  与 `runtime/routing/README.md`、SHA-256、许可证和版权说明一起跟踪。
- `runtime/routing/CXVPNRoutingHost.exe` 由 `routing-service/` 源码通过 `build_runtime.py` 重建，不跟踪。

## 初始化与远端

- 默认分支为 `main`。
- GitHub SSH 远端为 `git@github.com:SeanZhang97/cxvpn-tools.git`，本地名称为 `origin`。
- 首次提交或新增配置/运行时文件前，必须用 `git status --short --ignored` 复核跟踪和忽略边界，
  并对待提交文本做脱敏扫描。
