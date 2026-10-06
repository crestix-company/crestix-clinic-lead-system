@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
echo クリニック営業マスターの初回セットアップ
if exist ".venv\Scripts\python.exe" goto dependencies
py -3.12 -c "import sys" >nul 2>&1
if errorlevel 1 goto fallback
py -3.12 -m venv .venv
goto envcheck
:fallback
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)" >nul 2>&1
if errorlevel 1 goto missingpython
echo Python 3.12が見つからないため、導入済みのPython 3を使用します。検証済みの推奨版は3.12です。
py -3 -m venv .venv
:envcheck
if not exist ".venv\Scripts\python.exe" goto failed
:dependencies
".venv\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)" >nul 2>&1
if errorlevel 1 goto oldpython
".venv\Scripts\python.exe" -m pip install -r requirements-lock.txt
if errorlevel 1 goto failed
echo.
echo セットアップが完了しました。start_v2_windows.batをダブルクリックしてください。
pause
exit /b 0
:missingpython
echo Python 3.12以上が見つかりません。Python 3.12をインストールして再実行してください。
pause
exit /b 1
:oldpython
echo 現在の仮想環境はPython 3.12未満です。新しいフォルダーにZIPを展開し直してセットアップしてください。
pause
exit /b 1
:failed
echo セットアップを完了できませんでした。ネット接続・空き容量・フォルダーの保存権限を確認してください。
pause
exit /b 1
