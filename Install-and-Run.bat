@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher.ps1"
if errorlevel 1 (
  echo.
  echo HF Downloader could not start. See the error details above.
  pause
)
endlocal
