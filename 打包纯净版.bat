@echo off
rem Build a clean distribution archive without local credentials or sessions.
setlocal
set "ROOT=%~dp0"
set "APP_NAME=CXVPNTools"
set "APP_DIR=%ROOT%dist\%APP_NAME%"
set "OUT=%ROOT%dist\%APP_NAME%-clean.zip"
set "TAR=%SystemRoot%\System32\tar.exe"

if not exist "%APP_DIR%\%APP_NAME%.exe" (
    echo [ERROR] Missing dist\%APP_NAME%\%APP_NAME%.exe
    echo         Run: uv run --with pyinstaller python build.py
    pause
    exit /b 1
)

if not exist "%TAR%" (
    echo [ERROR] Missing system tar: %TAR%
    pause
    exit /b 1
)

tasklist /FI "IMAGENAME eq %APP_NAME%.exe" 2>nul | findstr /I /C:"%APP_NAME%.exe" >nul
if not errorlevel 1 (
    echo [1/4] Closing the running application...
    taskkill /F /IM "%APP_NAME%.exe" >nul 2>nul
    ping -n 3 127.0.0.1 >nul
)

echo [2/4] Removing the previous archive...
if exist "%OUT%" del /f /q "%OUT%"

echo [3/4] Creating the clean archive...
cd /d "%ROOT%dist"
"%TAR%" -a -c --options hdrcharset=UTF-8 -f "%APP_NAME%-clean.zip" ^
  --exclude="%APP_NAME%/config.json" ^
  --exclude="%APP_NAME%/run.log" ^
  --exclude="%APP_NAME%/startup.log" ^
  --exclude="%APP_NAME%/_internal/startup.log" ^
  --exclude="%APP_NAME%/_internal/webview_data" ^
  "%APP_NAME%"
if errorlevel 1 (
    echo [ERROR] Failed to create the archive.
    pause
    exit /b 1
)

echo [4/4] Checking excluded private files...
"%TAR%" -tf "%APP_NAME%-clean.zip" | findstr /I /C:"config.json" /C:"webview_data" /C:".log" >nul
if not errorlevel 1 (
    echo [ERROR] The archive contains a private file and must not be distributed.
    pause
    exit /b 1
)

for %%A in ("%OUT%") do echo [DONE] %%~fA ^(%%~zA bytes^)
pause
endlocal
