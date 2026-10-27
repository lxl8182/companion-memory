@echo off
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0"

rem 铃芽 RIN 一键启动：双击运行等同于 start.ps1 start
rem 需要停止时：在命令行执行 start.cmd stop

set "PS1=%~dp0start.ps1"
if not exist "%PS1%" (
    echo [x] 找不到 start.ps1
    pause
    exit /b 3
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %*
set "CODE=%ERRORLEVEL%"

if not "%CODE%"=="0" (
    echo.
    echo [x] 退出码 %CODE%
)
echo.
if "%~1"=="" pause
exit /b %CODE%
