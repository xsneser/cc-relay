@echo off
chcp 65001 >nul
title CC Relay - Windows 安装与卸载包一键构建流水线
setlocal enabledelayedexpansion

cd /d "%~dp0"

echo ======================================================================
echo              CC Relay - Windows 安装/卸载包完整构建流水线
echo   (包含 ASR 离线环境打包、SenseVoice 模型固化与伴侣组件协同卸载)
echo ======================================================================
echo.

:: 1. 检查本地 Python 环境
echo [1/6] 检查本地 Python 环境...
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未在系统 PATH 中检测到 Python，请先安装 Python 3.8+ 并勾选 "Add to PATH"。
    pause
    exit /b 1
)
python --version
echo [OK] Python 环境正常。
echo.

:: 2. 检查 Inno Setup 编译器 ISCC.exe
echo [2/6] 检查 Inno Setup 6 编译器 (ISCC.exe)...
set ISCC_PATH=""

where iscc >nul 2>&1
if %errorlevel% equ 0 (
    for /f "delims=" %%i in ('where iscc') do set ISCC_PATH="%%i"
)

if !ISCC_PATH!=="" (
    if exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" set ISCC_PATH="C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
    if exist "C:\Program Files\Inno Setup 6\ISCC.exe" set ISCC_PATH="C:\Program Files\Inno Setup 6\ISCC.exe"
    if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set ISCC_PATH="%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
)

if !ISCC_PATH!=="" (
    echo [提示] 未在系统中检测到 Inno Setup 6，正在尝试通过 Windows 包管理器 (winget) 自动安装...
    winget install JRSoftware.InnoSetup -e --accept-package-agreements --accept-source-agreements --silent
    if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set ISCC_PATH="%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
    if exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" set ISCC_PATH="C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
    if exist "C:\Program Files\Inno Setup 6\ISCC.exe" set ISCC_PATH="C:\Program Files\Inno Setup 6\ISCC.exe"
)

if !ISCC_PATH!=="" (
    echo.
    echo [错误] 未能找到 Inno Setup 6 编译器 (ISCC.exe)。
    echo 请手动前往官网下载安装 Inno Setup 6:
    echo   https://jrsoftware.org/isdl.php
    echo 安装完成后重新运行本脚本即可。
    echo.
    pause
    exit /b 1
)

echo [OK] 找到 Inno Setup 编译器: !ISCC_PATH!
echo.

:: 3. 编译主程序 cc-relay.exe
echo [3/6] 正在通过 PyInstaller 编译主程序 (dist\cc-relay.exe)...
python -m pip show pyinstaller >nul 2>&1
if %errorlevel% neq 0 (
    echo [提示] 正在安装 PyInstaller 打包依赖...
    python -m pip install --upgrade pyinstaller
)

python -m PyInstaller --clean -y cc-relay.spec
if %errorlevel% neq 0 (
    echo [错误] PyInstaller 编译主程序失败！
    pause
    exit /b 1
)
echo [OK] cc-relay.exe 编译成功。
echo.

:: 4. 组装 ASR 离线自包含运行环境与模型
echo [4/6] 正在组装 ASR 离线运行环境与 SenseVoice 模型...
python -m tools.voice_input.bundle_runtime
if %errorlevel% neq 0 (
    echo [错误] ASR 离线环境组装失败！
    pause
    exit /b 1
)
echo [OK] ASR 运行环境与模型已就绪。
echo.

:: 5. 组装发布暂存目录 (staging)
echo [5/6] 正在同步组装发布暂存目录 (installer_build\staging)...
python installer_build\prepare_staging.py
if %errorlevel% neq 0 (
    echo [错误] 暂存目录组装失败！
    pause
    exit /b 1
)
echo [OK] 发布暂存目录组装完毕。
echo.

:: 6. 执行 Inno Setup 深度压缩打包
echo [6/6] 正在调用 Inno Setup 编译最终安装包与卸载包...
if not exist "output" mkdir "output"

!ISCC_PATH! /O"output" installer.iss
if %errorlevel% neq 0 (
    echo [错误] Inno Setup 编译生成安装包失败！
    pause
    exit /b 1
)

echo.
echo ======================================================================
echo                      SUCCESS! 安装与卸载包构建成功
echo ======================================================================
for %%f in (output\*.exe) do (
    echo 生成的安装包: %%~dpnxf
    echo 文件大小: %%~zf 字节
)
echo.
echo 包含特性:
echo   1. 内置便携嵌入式 Python 运行时与 sherpa-onnx，无需系统安装 Python。
echo   2. 内置 SenseVoice 离线 INT8 语音大模型，用户安装即用、开箱即用。
echo   3. 内置 Codex (CLIProxyAPI) 代理网关。
echo   4. 安装自动写入用户 PATH 环境变量 (支持终端 claude 直通)。
echo   5. 卸载时原生弹窗询问用户是否同步卸载 Codex、Gemini (Anti) 和 ASR 语音环境。
echo   6. 卸载时自动拉起 Antigravity Tools 和 CPA 自身卸载 exe，实现无缝协同清理。
echo ======================================================================
echo.

pause
