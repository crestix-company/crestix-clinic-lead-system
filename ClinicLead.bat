@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

echo ========================================
echo Crestix Clinic Lead System
echo Windows one-click launcher
echo ========================================
echo.

if not exist "%~dp0scripts\update_and_launch_windows.ps1" (
  echo [ERROR] scripts\update_and_launch_windows.ps1 が見つかりません。
  echo GitHubの最新版を取得してから再実行してください。
  echo.
  pause
  exit /b 1
)

rem 旧手順で生成された既知の一時launcherがrepoをdirtyにして updater を止めるため、
rem 削除せずCrestixData配下へ退避する。未知のuntracked/modified fileは updater 側で引き続きSTOPする。
set "RUNTIME_DIR=%USERPROFILE%\CrestixData\clinic-lead\runtime\legacy"
if exist "%~dp0scripts\launch_v2_windows_temp.ps1" (
  if not exist "%RUNTIME_DIR%" mkdir "%RUNTIME_DIR%" >nul 2>&1
  move /Y "%~dp0scripts\launch_v2_windows_temp.ps1" "%RUNTIME_DIR%\launch_v2_windows_temp.ps1" >nul
  echo [INFO] 旧一時launcherをrepo外へ退避しました。
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\update_and_launch_windows.ps1"
set "EXITCODE=%ERRORLEVEL%"

if not "%EXITCODE%"=="0" (
  echo.
  echo [ERROR] 起動処理が停止しました。
  echo 上に表示されたエラー内容を前川へ共有してください。
  echo.
  pause
)

exit /b %EXITCODE%
