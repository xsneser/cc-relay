@echo off
chcp 65001 >nul
title CC Relay - 停止中转服务
setlocal enabledelayedexpansion

cd /d "%~dp0"

echo ======================================================================
echo                     正在停止 CC Relay 后台服务...
echo ======================================================================
echo.

set KILLED=0

:: 1. 尝试结束打包的 exe 进程
taskkill /F /IM cc-relay.exe >nul 2>&1
if %errorlevel%==0 (
    echo [OK] 已终止 cc-relay.exe 进程。
    set KILLED=1
)

:: 2. 尝试结束占用 8400 (API) 端口的后台进程
for /f "tokens=5" %%a in ('netstat -aon 2^>nul ^| findstr ":8400" ^| findstr "LISTENING"') do (
    if not "%%a"=="" (
        taskkill /F /PID %%a >nul 2>&1
        if !errorlevel!==0 (
            echo [OK] 已终止端口 8400 监听进程 (PID: %%a)。
            set KILLED=1
        )
    )
)

:: 3. 尝试结束占用 8610 (UI) 端口的后台进程
for /f "tokens=5" %%a in ('netstat -aon 2^>nul ^| findstr ":8610" ^| findstr "LISTENING"') do (
    if not "%%a"=="" (
        taskkill /F /PID %%a >nul 2>&1
        if !errorlevel!==0 (
            echo [OK] 已终止端口 8610 监听进程 (PID: %%a)。
            set KILLED=1
        )
    )
)

echo.
if %KILLED%==1 (
    echo ======================================================================
    echo [成功] CC Relay 中转与监控服务已全部停止。
    echo ======================================================================
) else (
    echo [提示] 未发现正在运行的 CC Relay 服务进程。
)

echo.
timeout /t 3 >nul
