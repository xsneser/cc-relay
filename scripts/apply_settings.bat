@echo off
cd /d "%~dp0.."
python scripts\apply_settings.py %*
pause
