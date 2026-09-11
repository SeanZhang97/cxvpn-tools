# Windows 安装包

使用 Inno Setup 6.7.3。编译器路径由构建脚本自动探测，也可以通过 `-Compiler` 指定。

## 构建

先准备 PyInstaller onedir 产物；程序没有变化时可复用已有的 dist/CXVPNTools。
如需重建主程序，按仓库 AGENTS.md 先退出程序，再执行标准构建。

    uv run --with pyinstaller python build.py

准备和验证依赖，再编译安装器（Windows PowerShell 5.1 或 PowerShell 7 均可）：

    powershell -NoProfile -ExecutionPolicy Bypass -File installer/fetch_prerequisites.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File installer/build_installer.ps1

编译器不在默认位置时，为 build_installer.ps1 指定 `-Compiler` 完整路径。
安装器版本从主 EXE 的 ProductVersion 读取，并与 core/version.py 核对。
不再单独维护安装脚本中的版本常量。

输出位于 dist/CXVPNTools-<版本>-setup.exe，并生成同名 .sha256 文件。
完整编译日志位于 build_tmp/installer-tools/compile.log。

## 离线依赖

两个完整安装包都必须存在，否则构建失败，不会生成缺少依赖的发布包：

- installer/prereqs/MicrosoftEdgeWebView2RuntimeInstallerX64.exe：Evergreen Standalone x64，
  可离线安装；不同于需要联网下载运行库的 Bootstrapper。
- installer/prereqs/VC_redist.x64.exe：Visual C++ v14 x64 Redistributable。

下载来源、固定下载地址、文件大小、SHA-256、安装器文件版本和 Microsoft 签名主体
保存在 prereqs.lock.json。构建时复核哈希、大小和 Authenticode 签名；安装前再次核对哈希。
文件本体通过 .gitignore 排除，可用 fetch_prerequisites.ps1 按锁定清单重新获取。
更新依赖需要重新核验微软签名和哈希后维护清单，不可直接覆盖成未知版本。

依赖安装包保留在所选安装目录的 prereqs 子目录。Evergreen 和 VC++ 实际运行库按微软机制
安装到系统共享位置，不支持由本安装器将其实际运行库根目录任意迁移；软件卸载不会删除
这些可能被其他软件使用的共享运行库。

## 安装行为

- 中文向导、可选择安装目录，默认 Program Files/CXVPNTools；支持 Windows 10+ x64 兼容系统。
- 固定 AppId 并沿用上次安装目录，1.0.0 及后续版本可直接覆盖升级；用户数据保留在 LocalAppData。
- 首次安装需要管理员权限，创建公共桌面及开始菜单快捷方式和标准卸载项。
- 所有用户安装模式检查 HKLM 的 WebView2 Runtime 有效版本；仅安装 Edge 浏览器不算满足依赖。
- VC++ 仅在缺失或版本低于所带安装包时安装；WebView2 已有有效机器级安装则跳过。
- 在复制主程序之前完成依赖安装和复检。失败、超时或复检失败停止安装；每个依赖执行限时 600 秒。
- 依赖返回需要重启时记录重启状态，不强制立即重启，也不在此时启动主程序。
- 升级时由 Inno Setup 处理旧程序的文件占用；用户数据不进入安装包，安装器复用现有 LocalAppData 数据路径。
- 主程序保持标准 PyInstaller 构建内容，本轮未重新修改或构建业务代码。

## 卸载交给用户验证

- 默认不勾选“删除用户配置、凭据和缓存”；静默卸载始终保留数据。
- 交互式卸载检测到主程序运行时，会让用户选择“强制终止并继续”或“取消卸载”；静默卸载不弹出选择框。
- 常规卸载只移除安装清单中的程序文件和快捷方式，不递归清空用户选择的整个安装目录。
- 卸载前有界停止并注销 CXVPNRoutingService；服务正常停止会调用其既有代理恢复流程。
- 勾选删除数据时清理对话框明确列出的当前用户 LocalAppData/CXVPNTools，
  以及 ProgramData/CXVPNManager/RoutingService。不会扫描其他用户目录。
- 使用其他管理员账号提权时，显示的 LocalAppData 属于该管理员；
  请先核对对话框路径，原登录用户的数据需由该用户自行清理。

## 验证范围

执行器测试使用临时无副作用 EXE，覆盖成功、重启要求、失败、超时、哈希不符：

    powershell -NoProfile -ExecutionPolicy Bypass -File tests/test_installer_prerequisite.ps1

已通过安装器编译、依赖签名与哈希核验、执行器离线测试，并打开最终安装包 /HELP 验证
引导程序能启动。尚未执行干净 Windows 虚拟机缺失依赖安装、实际安装/升级、卸载与
用户数据清理测试。本机没有可用 Windows Sandbox，本轮不执行真实卸载或清理用户数据。

安装日志：默认 Windows 临时目录 Setup Log ... .txt；可用 /LOG="完整路径" 指定。
依赖 worker 详细日志在安装期间的临时解压目录 prerequisite-*.log；服务清理异常日志
为临时目录 CXVPNTools-uninstall-service.log。
最终 CXVPNTools 安装包尚未使用产品 Authenticode 证书签名。

## 官方资料

- https://jrsoftware.org/isdl.php
- https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution
- https://developer.microsoft.com/en-us/microsoft-edge/webview2
- https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist
- 中文语言文件来自 jrsoftware/issrc 的 is-6_7_3 标签 Files/Languages/Unofficial/ChineseSimplified.isl；
  保留原作者署名，随仓库使用该版本。
