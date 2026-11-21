@echo off
rem ====================================================================
rem Remi-update.bat
rem
rem Put this file next to Remi.exe, in the folder that is managed by SVN or Git.
rem It stops Remi, updates the files (svn update / git pull), and starts Remi again.
rem
rem Usage:  Remi-update.bat [/nopause]
rem   /nopause  Do not wait for a key press when an error occurs
rem             (use this when the batch is started by a scheduled task).
rem ====================================================================
setlocal EnableExtensions

rem ---- Settings (edit here if needed) ----------------------------------
rem (A value that is already set in the environment is used as it is.)
rem VCS: auto (detect SVN or Git), git, svn, or none (do not update the files)
if not defined VCS set "VCS=auto"
rem Arguments for Remi.exe when it is started again. Example: --background
rem (starts hidden in the tray, the same as the automatic start with Windows)
if not defined START_ARGS set "START_ARGS="
rem Arguments that make a running Remi quit (normally you do not need to change this)
if not defined QUIT_ARGS set "QUIT_ARGS=--quit"
rem Seconds to wait for Remi to exit. After that, Remi is terminated.
if not defined WAIT_SECONDS set "WAIT_SECONDS=30"
rem ------------------------------------------------------------------------

set "EXE=Remi.exe"
set "RC=0"
cd /d "%~dp0"
if not exist "%EXE%" (
  set "RC=1"
  goto :missing
)

echo [1/3] Stopping Remi ...
rem Remi hides in the tray when its window is closed, so ask it to quit by itself.
rem (Nothing happens if Remi is not running.)
start "" /wait "%EXE%" %QUIT_ARGS%
set /a WAITED=0
:wait_exit
tasklist /FI "IMAGENAME eq %EXE%" /NH 2>nul | find /I "%EXE%" >nul
if errorlevel 1 goto :stopped
if %WAITED% GEQ %WAIT_SECONDS% goto :force
set /a WAITED+=1
ping -n 2 127.0.0.1 >nul
goto :wait_exit
:force
echo Remi did not exit within %WAIT_SECONDS% seconds. Terminating it.
taskkill /F /IM "%EXE%" >nul 2>&1
ping -n 3 127.0.0.1 >nul
:stopped

echo [2/3] Updating the files ...
if /I "%VCS%"=="auto" call :detect
if /I "%VCS%"=="git" (
  git pull --ff-only
  if errorlevel 1 set "RC=2"
) else if /I "%VCS%"=="svn" (
  svn update --non-interactive
  if errorlevel 1 set "RC=2"
) else (
  echo The update was skipped. No SVN or Git working copy was found, or VCS is set to none.
)

echo [3/3] Starting Remi ...
start "" "%EXE%" %START_ARGS%

:finish
if not "%RC%"=="0" (
  echo.
  echo The update did not finish correctly [code %RC%]. See the messages above.
  if /I not "%~1"=="/nopause" pause
)
exit /b %RC%

:missing
echo %EXE% was not found in the folder of this batch file.
goto :finish

:detect
set "VCS=none"
git rev-parse --is-inside-work-tree >nul 2>&1
if not errorlevel 1 (
  set "VCS=git"
  goto :eof
)
svn info >nul 2>&1
if not errorlevel 1 set "VCS=svn"
goto :eof
