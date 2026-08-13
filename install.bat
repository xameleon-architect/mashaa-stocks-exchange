@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo Python launcher not found. Install Python 3.11 or newer from python.org.
  pause
  exit /b 1
)
py -3 -m venv .venv
if errorlevel 1 exit /b 1
if not exist ".env" copy ".env.example" ".env" >nul
echo.
echo Installed. Open .env in Notepad and fill in the four values.
pause
