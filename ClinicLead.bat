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
