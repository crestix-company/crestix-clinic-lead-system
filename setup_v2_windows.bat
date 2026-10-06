@echo off
setlocal
cd /d "%~dp0"
echo Setting up Clinic Lead...
if exist ".venv\Scripts\python.exe" goto dependencies
py -3.12 -c "import sys" >nul 2>&1
if errorlevel 1 goto fallback
py -3.12 -m venv .venv
goto envcheck
:fallback
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)" >nul 2>&1
if errorlevel 1 goto missingpython
echo Python 3.12 was not found. Trying the installed Python 3. Python 3.12 is recommended.
py -3 -m venv .venv
:envcheck
if not exist ".venv\Scripts\python.exe" goto failed
:dependencies
".venv\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)" >nul 2>&1
if errorlevel 1 goto oldpython
".venv\Scripts\python.exe" -m pip install -r requirements-lock.txt
if errorlevel 1 goto failed
echo.
echo Setup completed. Run start_v2_windows.bat.
pause
exit /b 0
:missingpython
echo Python 3.12 or later was not found. Install Python 3.12 and run setup again.
pause
exit /b 1
:oldpython
echo The existing virtual environment uses Python older than 3.12.
echo Extract the ZIP into a new folder and run setup again.
pause
exit /b 1
:failed
echo Setup failed. Check the network, free disk space, and folder permissions.
pause
exit /b 1
