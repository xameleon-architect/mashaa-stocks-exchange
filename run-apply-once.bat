@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run install.bat first.
  pause
  exit /b 1
)
echo WARNING: this command writes stock values to the TEST Tilda catalog.
set /p CONFIRM=Type APPLY-TEST-TILDA and press Enter: 
if not "%CONFIRM%"=="APPLY-TEST-TILDA" (
  echo Cancelled.
  pause
  exit /b 2
)
".venv\Scripts\python.exe" sync.py --apply --confirm APPLY-TEST-TILDA --verbose
set EXIT_CODE=%ERRORLEVEL%
echo.
echo Exit code: %EXIT_CODE%
pause
exit /b %EXIT_CODE%

