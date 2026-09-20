@echo off
rem ============================================================
rem  CLIProxyAPI launcher (hidden, no console window)
rem  Place next to cli-proxy-api.exe and config.yaml
rem ============================================================
setlocal
cd /d "%~dp0"
powershell -NoProfile -Command "$p = Get-Process cli-proxy-api -ErrorAction SilentlyContinue; if (-not $p) { Start-Process -WindowStyle Hidden -FilePath '%~dp0cli-proxy-api.exe' -ArgumentList '--config','%~dp0config.yaml' -WorkingDirectory '%~dp0' -RedirectStandardOutput '%~dp0proxy.out.log' -RedirectStandardError '%~dp0proxy.err.log' }"
