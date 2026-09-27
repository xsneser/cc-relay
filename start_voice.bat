@echo off
title Claude Code Voice Companion
cd /d "%~dp0"

for /f "delims=" %%i in ('python -c "from tools.voice_input.runtime import find_voice_python; print(find_voice_python())" 2^>nul') do set "PY_EXE=%%i"
if "%PY_EXE%"=="" set "PY_EXE=python"

echo [*] Launching Voice Companion service with: %PY_EXE%
"%PY_EXE%" -m tools.voice_input service %*
if errorlevel 1 pause
