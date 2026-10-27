@echo off
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0"
if not exist "%~dp0start.ps1" (
    echo [x] find start.ps1 failed
    pause
    exit /b 3
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" -Action stop %*
set "CODE=%ERRORLEVEL%"
echo.
pause
exit /b %CODE%
