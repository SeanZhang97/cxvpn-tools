@echo off
rem ============================================================
rem  打包纯净版.bat - 打一个不含个人数据的分发 zip 包
rem
rem  来源: dist\CXVPN管理器 (PyInstaller onedir 便携目录, 整个文件夹可分发)
rem  产物: dist\CXVPN管理器-纯净版.zip
rem  剔除: config.json(含账号密码与密钥) / webview_data(登录会话缓存) / 运行日志
rem  依赖: Windows 10 1803+ 自带的 C:\Windows\System32\tar.exe
rem  编码: 本脚本按中文系统默认编码(GBK)保存, 请勿转成 UTF-8
rem  注意:
rem    1. echo 文本一律使用全角括号, 半角括号会破坏 if 代码块解析
rem    2. tar 固定用系统自带版本(不同机器 PATH 里可能装了 Git 等其他 tar,
rem       各版本对参数支持不一致, 其中 --force-local 不受支持)
rem    3. 必须先 cd 到 dist 用相对路径调用 tar, 否则 bsdtar 会把绝对路径里
rem       的盘符 C: 误认为远程主机地址(Cannot connect to C)
rem    4. 必须加 hdrcharset=UTF-8, 否则中文文件名以本机 GBK 字节写入,
rem       换到非中文系统的电脑解压会变成乱码
rem ============================================================
setlocal enabledelayedexpansion
set "ROOT=%~dp0"
set "APP_DIR=%ROOT%dist\CXVPN管理器"
set "OUT=%ROOT%dist\CXVPN管理器-纯净版.zip"
set "TAR=%SystemRoot%\System32\tar.exe"

if not exist "%APP_DIR%\CXVPN管理器.exe" (
    echo [错误] 未找到 dist\CXVPN管理器\CXVPN管理器.exe
    echo        请先在本目录执行打包命令: uv run --with pyinstaller python build.py
    pause
    exit /b 1
)

if not exist "%TAR%" (
    echo [错误] 未找到系统自带 tar: %TAR%
    echo        本脚本需要 Windows 10 1803 及以上版本
    pause
    exit /b 1
)

rem 1. 程序在运行则强制关闭, 避免个人数据在压缩过程中被改写
tasklist /FI "IMAGENAME eq CXVPN管理器.exe" 2>nul | findstr /I "CXVPN管理器.exe" >nul
if not errorlevel 1 (
    echo [1/4] 程序正在运行, 先强制关闭...
    taskkill /F /IM "CXVPN管理器.exe" >nul 2>nul
    ping -n 3 127.0.0.1 >nul
)

rem 2. 清理旧压缩包
echo [2/4] 清理旧压缩包...
if exist "%OUT%" del /f /q "%OUT%"

rem 3. 压缩 (排除个人数据)
echo [3/4] 正在压缩, 请稍候 (约需数十秒)...
cd /d "%ROOT%dist"
"%TAR%" -a -c --options hdrcharset=UTF-8 -f "CXVPN管理器-纯净版.zip" ^
  --exclude="CXVPN管理器/config.json" ^
  --exclude="CXVPN管理器/run.log" ^
  --exclude="CXVPN管理器/startup.log" ^
  --exclude="CXVPN管理器/_internal/startup.log" ^
  --exclude="CXVPN管理器/_internal/webview_data" ^
  "CXVPN管理器"
if errorlevel 1 (
    echo [错误] 压缩失败, 请检查 dist 目录是否完整
    pause
    exit /b 1
)

rem 4. 校验: 压缩包内不应残留个人数据文件
echo [4/4] 校验压缩包内容...
"%TAR%" -tf "CXVPN管理器-纯净版.zip" | findstr /I /C:"config.json" /C:"webview_data" /C:".log" >nul
if errorlevel 1 (
    echo [通过] 未发现 config.json / webview_data / 日志文件残留
    goto :show_result
)
echo [警告] 压缩包内发现疑似个人数据文件, 请勿直接分发, 先检查排除规则:
"%TAR%" -tf "CXVPN管理器-纯净版.zip" | findstr /I /C:"config.json" /C:"webview_data" /C:".log"

:show_result
if exist "%OUT%" (
    for %%A in ("%OUT%") do echo [完成] 产物: %%~nxA 大小 %%~zA 字节
    echo [说明] 对方电脑需 Win10 x64 及以上, 并已安装 WebView2 或 Edge
    echo        （Win11 自带, 通常无需处理; 未签名程序首次运行可能被 SmartScreen 拦截）
) else (
    echo [错误] 压缩包未生成, 请检查 dist 目录
)
pause
endlocal