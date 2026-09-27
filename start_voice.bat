@echo off
title Claude Code Voice Companion
cd /d "%~dp0"

set "PY_EXE=tools\voice_input\.venv\Scripts\python.exe"
if not exist "%PY_EXE%" (
    set "PY_EXE=python"
)

echo [*] Launching Voice Companion service...
"%PY_EXE%" -m tools.voice_input service %*
if errorlevel 1 pause
