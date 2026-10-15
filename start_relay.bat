@echo off
chcp 65001 >nul
title CC Relay - 启动中转服务
setlocal

cd /d "%~dp0"

if not exist "main_launcher.py" (
    echo [错误] 未在当前目录下找到 main_launcher.py，请检查运行路径。
    pause
    exit /b 1
)

:: 优先使用 pythonw 静默启动，其次回退使用 python
where pythonw >nul 2>&1
if not errorlevel 1 (
    start "" pythonw main_launcher.py
    exit /b 0
)

where python >nul 2>&1
if not errorlevel 1 (
    start "" python main_launcher.py
    exit /b 0
)

echo [错误] 未检测到 Python 运行环境。
echo 请安装 Python 3.8+ 并确保将其添加至系统环境变量 PATH 中。
pause
exit /b 1
