@echo off
title Claude Code Voice Companion
cd /d "%~dp0..\.."

for /f "delims=" %%i in ('python -c "from tools.voice_input.runtime import find_voice_python; print(find_voice_python())" 2^>nul') do set "PY_EXE=%%i"
if "%PY_EXE%"=="" set "PY_EXE=python"

:: 自动自检并静默补齐缺失依赖
"%PY_EXE%" -c "from tools.voice_input.runtime import check_deps_ready; exit(0 if check_deps_ready() else 1)" 2>nul
if errorlevel 1 (
    echo [*] Checking and self-healing voice companion dependencies...
    "%PY_EXE%" -m tools.voice_input.runtime --ensure-deps
)

echo [*] Launching Voice Companion with: %PY_EXE%
"%PY_EXE%" -m tools.voice_input service %*
if errorlevel 1 pause
