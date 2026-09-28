@echo off
title Claude Code Voice Companion Setup
cd /d "%~dp0"

echo =================================================================
echo   Claude Code CLI 语音输入伴侣 - 一键环境与模型配置
echo =================================================================
echo.

python tools\voice_input\install_voice.py %*
if errorlevel 1 (
    echo.
    echo [-] 配置过程中遇到错误，请检查上方的提示信息。
)
pause
