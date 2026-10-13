@echo off
setlocal
rem ASCII-only on purpose: UTF-8 Japanese text after "chcp 65001" breaks cmd.exe line parsing.
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" goto missing
".venv\Scripts\python.exe" -c "import streamlit,pandas,openpyxl,yaml,requests,bs4,rapidfuzz,filelock,tzdata" >nul 2>&1
if errorlevel 1 goto missing
echo Starting Clinic Lead System. If the browser does not open, paste the URL shown below into the address bar.
echo Keep this window open while you use the app. Press Ctrl+C to stop.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\update_and_launch_windows.ps1"
if errorlevel 1 pause
exit /b
:missing
echo First-time setup is required. Run setup_v2_windows.ps1 (or setup_v2_windows.bat) first.
pause
exit /b 1
