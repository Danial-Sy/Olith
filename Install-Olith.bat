@echo off
setlocal
cd /d "%~dp0"

where powershell.exe >nul 2>nul
if errorlevel 1 (
  echo ERROR: Windows PowerShell was not found.
  echo Olith requires Windows 10 or Windows 11.
  pause
  exit /b 1
)

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install-windows.ps1" %*
set "OLITH_SETUP_EXIT=%ERRORLEVEL%"
if not "%OLITH_SETUP_EXIT%"=="0" (
  echo.
  echo Olith setup did not finish. Review the error above and run this file again.
)
exit /b %OLITH_SETUP_EXIT%
