@echo off
chcp 936 >nul
setlocal EnableExtensions EnableDelayedExpansion

REM ===========================================================================
REM  RIN stack launcher (new chain):
REM    service 48931 + companion_host 48911, then optional Steam shell.
REM
REM  Replaces start-neko-source-shell.cmd, which drives the OLD embedded engine
REM  chain (COMPANION_MEMORY_ENGINE). The new chain runs the standalone service
REM  as the only core owner and the host as a remote client.
REM  Never run both at once: they use the same 48911-48916 ports.
REM
REM  Steam launch option:
REM    cmd /c "F:\game\projectNEKO\rin-memory\start-rin-stack.cmd" %command%
REM
REM  Overridable environment variables:
REM    RIN_CONFIG   stack.toml              (default companion-runtime\config\stack.toml)
REM    RIN_PYTHON   rin-memory python (default rin-memory\.venv)
REM    NEKO_SHELL   shell exe, or "none" to skip the shell
REM    RIN_WAIT     seconds to wait for the host (default 180)
REM ===========================================================================

if not defined RIN_ROOT     set "RIN_ROOT=F:\game\projectNEKO\rin-memory"
if not defined RIN_CONFIG   set "RIN_CONFIG=F:\game\projectNEKO\companion-runtime\config\stack.toml"
if not defined RIN_PYTHON   set "RIN_PYTHON=%RIN_ROOT%\.venv\Scripts\python.exe"
if not defined NEKO_SHELL   set "NEKO_SHELL=F:\steam\steamapps\common\N.E.K.O\N.E.K.O.exe"
if not defined RIN_WAIT     set "RIN_WAIT=180"
if not defined RIN_LOG      set "RIN_LOG=F:\game\projectNEKO\companion-runtime\logs\start-rin-stack.log"

echo [rin] config = %RIN_CONFIG%
echo [rin] python = %RIN_PYTHON%
echo [rin] shell  = %NEKO_SHELL%
call :log "start config=%RIN_CONFIG% shell=%NEKO_SHELL%"

if not exist "%RIN_PYTHON%" goto :no_python
if not exist "%RIN_CONFIG%" goto :no_config

call :port_busy
set "RIN_PORTS_BUSY=%errorlevel%"
if "%RIN_PORTS_BUSY%"=="1" goto :ports_busy

echo [rin] starting service and host ...
call :stack_start
set "RIN_START=%errorlevel%"
if not "%RIN_START%"=="0" goto :stack_failed

echo [rin] waiting for host ...
call :wait_host
set "RIN_WAITED=%errorlevel%"
if not "%RIN_WAITED%"=="0" goto :host_slow

call :chain_ok
set "RIN_CHAIN=%errorlevel%"
if not "%RIN_CHAIN%"=="0" goto :not_connected

echo [rin] ready: service 48931 / host 48911 connected
call :log "chain ready"

if /I "%NEKO_SHELL%"=="none" goto :no_shell
if "%~1"=="" goto :shell_default

echo [rin] launching shell (Steam command line) ...
start "" %*
call :log "shell launched (steam)"
goto :done

:shell_default
echo [rin] launching shell (default path) ...
start "" "%NEKO_SHELL%"
call :log "shell launched (default)"
goto :done

:no_shell
echo [rin] NEKO_SHELL=none, service and host only.
call :log "shell skipped"
goto :done

:done
endlocal
exit /b 0

:no_python
echo [rin] ERROR: missing rin-memory python: %RIN_PYTHON%
call :log "ERROR missing python"
goto :fail

:no_config
echo [rin] ERROR: missing config: %RIN_CONFIG%
call :log "ERROR missing config"
goto :fail

:ports_busy
echo [rin] ERROR: a process already listens on 48911-48916.
echo [rin]        The old chain uses the same ports; close N.E.K.O and its backend first.
call :log "ERROR ports busy; refusing to start"
goto :fail

:stack_failed
echo [rin] ERROR: stack start failed.
call :log "ERROR stack start failed"
goto :fail

:host_slow
echo [rin] ERROR: host not ready in %RIN_WAIT%s.
echo [rin]        log: F:\game\projectNEKO\companion-runtime\logs\neko-host.log
call :log "ERROR host not ready"
goto :fail

:not_connected
echo [rin] ERROR: 48911 answers but is not connected to the RIN service.
echo [rin]        check the [host] section in stack.toml and neko-host.log.
call :log "ERROR host not connected"
goto :fail

:fail
if "%~1"=="" pause >nul
endlocal
exit /b 1

REM ---------------------------------------------------------------------------
:log
if not exist "F:\game\projectNEKO\companion-runtime\logs" mkdir "F:\game\projectNEKO\companion-runtime\logs" >nul 2>&1
>>"%RIN_LOG%" echo [%date% %time%] %~1
exit /b 0

REM errorlevel 1 when any host port is already listening.
rem `netstat -ano` is used instead of a PowerShell one-liner inside `for /f`:
REM the nested quoting there returned a value that did not reflect the real
REM state, so the launcher refused to start on a free port.
:port_busy
rem The pipeline's exit status comes from the LAST command (findstr), which is
rem reliable; using `if not errorlevel 1` right after a pipe in a called label
rem picked up the wrong code. Capture it into a variable instead.
netstat -ano -p tcp | findstr /C:"LISTENING" | findstr /C:":48911 " /C:":48912 " /C:":48915 " /C:":48916 " >nul 2>&1
set "BUSY=%errorlevel%"
if "%BUSY%"=="0" exit /b 1
exit /b 0

:stack_start
"%RIN_PYTHON%" -m rin_memory.stack start --config "%RIN_CONFIG%" --no-open >nul 2>&1
exit /b %errorlevel%

:wait_host
set /a "_t=0"
:wait_loop
set "HCODE="
for /f %%i in ('curl -s -o NUL -w "%%{http_code}" -m 3 "http://127.0.0.1:48911/health" 2^>nul') do set "HCODE=%%i"
if "!HCODE!"=="200" exit /b 0
set /a "_t+=2"
if !_t! GEQ %RIN_WAIT% exit /b 1
timeout /t 2 /nobreak >nul 2>&1
goto :wait_loop

REM Matching JSON from cmd is unreliable (braces/pipes/quotes); delegate.
:chain_ok
set "RIN_OK=0"
for /f %%i in ('powershell -NoProfile -Command "try { $r = Invoke-RestMethod -Uri 'http://127.0.0.1:48911/api/rin-memory/status' -TimeoutSec 5; if ($r.status -eq 'connected') { '1' } else { '0' } } catch { '0' }" 2^>nul') do set "RIN_OK=%%i"
if not "%RIN_OK%"=="1" exit /b 1
exit /b 0
