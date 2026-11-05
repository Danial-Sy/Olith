@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Run Install-Olith.bat first.
  pause
  exit /b 1
)
if exist "%~dp0Olith.vbs" (
  wscript.exe //nologo "%~dp0Olith.vbs"
  exit /b 0
)
start "" ".venv\Scripts\pythonw.exe" desktop.py
