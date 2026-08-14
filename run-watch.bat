@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run install.bat first.
  pause
  exit /b 1
)
echo Continuous diagnostic dry-run will run every 60 seconds.
echo No data will be sent to Tilda.
".venv\Scripts\python.exe" sync.py --watch
set EXIT_CODE=%ERRORLEVEL%
echo.
echo Exit code: %EXIT_CODE%
pause
exit /b %EXIT_CODE%
