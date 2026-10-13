@echo off
setlocal
rem ASCII-only on purpose: UTF-8 Japanese text after "chcp 65001" breaks cmd.exe line parsing.
chcp 65001 >nul
cd /d "%~dp0"

echo ========================================
echo Crestix Clinic Lead System
echo Windows one-click launcher
echo ========================================
echo.

if not exist "%~dp0scripts\update_and_launch_windows.ps1" (
  echo [ERROR] scripts\update_and_launch_windows.ps1 was not found.
  echo Get the latest version from GitHub and run this again.
  echo.
  pause
  exit /b 1
)

rem A known temporary launcher created by an old procedure makes the repo dirty and stops the updater.
rem Move it (do not delete it) under CrestixData. Unknown untracked/modified files still STOP in the updater.
set "RUNTIME_DIR=%USERPROFILE%\CrestixData\clinic-lead\runtime\legacy"
if exist "%~dp0scripts\launch_v2_windows_temp.ps1" (
  if not exist "%RUNTIME_DIR%" mkdir "%RUNTIME_DIR%" >nul 2>&1
  move /Y "%~dp0scripts\launch_v2_windows_temp.ps1" "%RUNTIME_DIR%\launch_v2_windows_temp.ps1" >nul
  echo [INFO] Moved the old temporary launcher out of the repository.
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\update_and_launch_windows.ps1"
set "EXITCODE=%ERRORLEVEL%"

if not "%EXITCODE%"=="0" (
  echo.
  echo [ERROR] Startup stopped.
  echo Please share the error message shown above with the administrator.
  echo.
  pause
)

exit /b %EXITCODE%
