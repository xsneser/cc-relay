@echo off
chcp 65001 >nul
title CC Relay - 启动中转服务
setlocal

cd /d "%~dp0"

:: 优先检测是否存在编译后的独立 exe
if exist "cc-relay.exe" (
    start "" "cc-relay.exe"
    exit /b 0
)

if exist "dist\cc-relay.exe" (
    start "" "dist\cc-relay.exe"
    exit /b 0
)

:: 检查本地 pythonw
where pythonw >nul 2>&1
if %errorlevel%==0 (
    start "" pythonw main_launcher.py
    exit /b 0
)

:: 回退到标准 python
where python >nul 2>&1
if %errorlevel%==0 (
    start "" python main_launcher.py
    exit /b 0
)

echo [错误] 未检测到 Python 环境或已编译的 cc-relay.exe。
echo 请先运行 build_exe.bat 编译独立程序，或者安装 Python 3.8+。
pause
exit /b 1
