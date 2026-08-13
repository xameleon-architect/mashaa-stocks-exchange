@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run install.bat first.
  pause
  exit /b 1
)
echo WARNING: continuous TEST synchronization will run every 60 seconds.
set /p CONFIRM=Type APPLY-TEST-TILDA and press Enter: 
if not "%CONFIRM%"=="APPLY-TEST-TILDA" (
  echo Cancelled.
  pause
  exit /b 2
)
".venv\Scripts\python.exe" sync.py --watch --apply --confirm APPLY-TEST-TILDA

