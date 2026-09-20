@echo off
chcp 65001 >nul
title CC Relay - Windows EXE 打包构建工具
setlocal enabledelayedexpansion

cd /d "%~dp0"

echo ======================================================================
echo           CC Relay - 独立 Windows 可执行程序打包工具 (PyInstaller)
echo ======================================================================
echo.

:: 1. 检测 Python 环境
echo [1/4] 检查本地 Python 环境...
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未在系统 PATH 中检测到 Python，请先安装 Python 3.8+ 并勾选 "Add to PATH"。
    echo.
    pause
    exit /b 1
)

python --version
echo [OK] Python 环境正常。
echo.

:: 2. 检测并安装 PyInstaller
echo [2/4] 检查 PyInstaller 打包套件...
python -m pip show pyinstaller >nul 2>&1
if %errorlevel% neq 0 (
    echo [提示] 尚未检测到 PyInstaller，正在通过 pip 自动安装...
    python -m pip install --upgrade pyinstaller
    if %errorlevel% neq 0 (
        echo [错误] 安装 PyInstaller 失败，请检查网络连接或尝试以管理员身份运行。
        echo.
        pause
        exit /b 1
    )
) else (
    echo [OK] PyInstaller 已就绪。
)
echo.

:: 3. 开始执行打包
echo [3/4] 开始执行 PyInstaller 独立编译打包...
echo [说明] 正在生成单文件 cc-relay.exe (后台静默无控制台黑框模式)...
echo.

python -m PyInstaller --clean -y cc-relay.spec
if %errorlevel% neq 0 (
    echo.
    echo [错误] 打包失败，请查看上方 PyInstaller 的详细输出日志。
    echo.
    pause
    exit /b 1
)

:: 4. 组装便携运行目录
echo.
echo [4/4] 正在组装 dist 发布目录资源...

if not exist "dist" mkdir "dist"

:: 复制辅助脚本与模板至 dist 目录
if exist "ui.html" copy /y "ui.html" "dist\" >nul
if exist "config.example.json" copy /y "config.example.json" "dist\" >nul
if exist "stop_relay.bat" copy /y "stop_relay.bat" "dist\" >nul

echo.
echo ======================================================================
echo                    SUCCESS! 打包编译全部完成
echo ======================================================================
echo 生成的独立程序位于:
echo   %~dp0dist\cc-relay.exe
echo.
echo 使用说明:
echo   1. 直接双击 dist\cc-relay.exe 即可运行！
echo   2. 后台静默启动，不弹出黑框终端，自动打开浏览器深色监控面板。
echo   3. 若已在运行，重复双击会自动唤醒当前面板，不会重复占用端口。
echo   4. 如需停止后台服务，双击运行 stop_relay.bat 即可一键停止。
echo ======================================================================
echo.

pause
