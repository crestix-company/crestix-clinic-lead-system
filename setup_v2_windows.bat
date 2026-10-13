@echo off
setlocal
rem ASCII-only on purpose: UTF-8 Japanese text after "chcp 65001" breaks cmd.exe line parsing.
chcp 65001 >nul
cd /d "%~dp0"
echo Clinic Lead System - first-time setup
if exist ".venv\Scripts\python.exe" goto dependencies
py -3.12 -c "import sys" >nul 2>&1
if errorlevel 1 goto fallback
py -3.12 -m venv .venv
goto envcheck
:fallback
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)" >nul 2>&1
if errorlevel 1 goto missingpython
echo Python 3.12 was not found, so the installed Python 3 is used. The verified version is 3.12.
py -3 -m venv .venv
:envcheck
if not exist ".venv\Scripts\python.exe" goto failed
:dependencies
".venv\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)" >nul 2>&1
if errorlevel 1 goto oldpython
".venv\Scripts\python.exe" -m pip install -r requirements-lock.txt
if errorlevel 1 goto failed
echo.
echo Setup finished. Double-click start_v2_windows.bat to start.
pause
exit /b 0
:missingpython
echo Python 3.12 or later was not found. Install Python 3.12 and run this again.
pause
exit /b 1
:oldpython
echo The current virtual environment is older than Python 3.12. Extract the ZIP into a new folder and set it up again.
pause
exit /b 1
:failed
echo Setup could not be completed. Check the network connection, free disk space and folder write permission.
pause
exit /b 1
