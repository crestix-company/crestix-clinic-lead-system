@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" goto missing
".venv\Scripts\python.exe" -c "import streamlit,pandas,openpyxl,yaml,requests,bs4,rapidfuzz,filelock,tzdata" >nul 2>&1
if errorlevel 1 goto missing
echo Starting Clinic Lead...
echo Keep this window open. Press Ctrl+C to stop.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\update_and_launch_windows.ps1"
if errorlevel 1 pause
exit /b
:missing
echo Initial setup is required. Run setup_v2_windows.bat first.
pause
exit /b 1
