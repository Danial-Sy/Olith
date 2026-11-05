@echo off
setlocal
cd /d "%~dp0"

rem Update Olith from its latest GitHub release. Safe to double-click: it
rem checks first and does nothing when this copy is already current.

where py >nul 2>nul
if not errorlevel 1 goto :usepy
where python >nul 2>nul
if not errorlevel 1 goto :usepython

echo ERROR: Python 3.11+ was not found.
echo Install it from https://www.python.org/downloads/ or run Install-Olith.bat first.
pause
exit /b 1

:usepy
py -3 update.py %*
goto :done

:usepython
python update.py %*
goto :done

:done
set "OLITH_UPDATE_EXIT=%ERRORLEVEL%"
if not "%OLITH_UPDATE_EXIT%"=="0" (
  echo.
  echo The update did not finish. Review the error above and run this file again.
)
pause
exit /b %OLITH_UPDATE_EXIT%
