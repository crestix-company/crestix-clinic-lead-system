@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" goto missing
".venv\Scripts\python.exe" -c "import streamlit,pandas,openpyxl,yaml,requests,bs4,rapidfuzz,filelock,tzdata" >nul 2>&1
if errorlevel 1 goto missing
echo 起動中です。ブラウザーが開かない場合は、表示されるURLをアドレスバーに貼り付けてください。
echo 終了するまでこの画面を閉じないでください。終了はCtrl+Cです。
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\update_and_launch_windows.ps1"
if errorlevel 1 pause
exit /b
:missing
echo 初回セットアップが必要です。先にsetup_v2_windows.batをダブルクリックしてください。
pause
exit /b 1
