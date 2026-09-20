@echo off
rem ============================================================
rem  claude lifecycle wrapper (cc-relay)
rem  enter : ensure cc-relay(+UI) running; open UI page
rem  watch : stop relay + codex when LAST claude exits
rem  bypass: set CLAUDE_SKIP_AUTO=1
rem  keep this file ASCII-only and CRLF-terminated
rem ============================================================
setlocal
if "%CLAUDE_SKIP_AUTO%"=="1" goto run
if "%CC_RELAY_DIR%"=="" set "CC_RELAY_DIR=%~dp0.."
python "%CC_RELAY_DIR%\lifecycle.py" autostart
set RC=%errorlevel%
if %RC%==0 start "" "http://127.0.0.1:8610"
start "" pythonw "%CC_RELAY_DIR%\lifecycle.py" watch
:run
call "%APPDATA%\npm\claude.cmd" %*
