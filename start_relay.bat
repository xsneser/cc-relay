@echo off
chcp 65001 >nul
title CC Relay - 启动中转服务
setlocal

cd /d "%~dp0"

:: 1. 源码模式优先：如果存在 main_launcher.py，优先使用本地 Python 运行最新源码
if exist "main_launcher.py" (
    where pythonw >nul 2>&1
    if %errorlevel%==0 (
        start "" pythonw main_launcher.py
        exit /b 0
    )
    where python >nul 2>&1
    if %errorlevel%==0 (
        start "" python main_launcher.py
        exit /b 0
    )
)

:: 2. 发行版/二进制模式：如果同级目录下存在已构建的独立 exe
if exist "cc-relay.exe" (
    start "" "cc-relay.exe"
    exit /b 0
)

:: 3. 如果在源码目录下未检测到 Python，但存在 dist\cc-relay.exe
if exist "dist\cc-relay.exe" (
    echo [提示] 未检测到 Python 环境，正在尝试启动 dist\cc-relay.exe ...
    start "" "dist\cc-relay.exe"
    exit /b 0
)

echo [错误] 未检测到 Python 环境或可执行程序。
if exist "main_launcher.py" (
    echo 当前处于源码目录，请安装 Python 3.8+ 或运行 scripts\build_exe.bat 打包程序。
) else (
    echo 未找到 cc-relay.exe，请检查安装完整性。
)
pause
exit /b 1
