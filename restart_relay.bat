@echo off
chcp 65001 >nul
title CC Relay - 重启中转服务
setlocal

cd /d "%~dp0"

echo ======================================================================
echo                     正在重启 CC Relay 服务...
echo ======================================================================
echo.

:: 设置 CC_RELAY_NO_BROWSER=1，防止重启时自动打开重复的浏览器标签
set CC_RELAY_NO_BROWSER=1
set CC_RELAY_RESTART=1

:: 1. 停止当前运行的中转服务
call "%~dp0stop_relay.bat"

:: 2. 等待端口释放缓冲
ping -n 3 127.0.0.1 >nul

:: 3. 启动中转服务
call "%~dp0start_relay.bat"

echo.
echo ======================================================================
echo [完成] CC Relay 服务已重启。
echo ======================================================================
exit /b 0
