@echo off
title Claude Code Voice Companion
cd /d "%~dp0..\.."

if not exist "tools\voice_input\.venv\Scripts\python.exe" (
    echo [!] Virtual environment not found. Running setup first...
    python tools\voice_input\install_voice.py
)

tools\voice_input\.venv\Scripts\python.exe -m tools.voice_input listen --hotkey mouse_x1
if errorlevel 1 pause
