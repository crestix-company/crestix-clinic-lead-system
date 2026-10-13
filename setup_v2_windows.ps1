<# 
Clinic Lead System - Windows first-time setup (PowerShell 5.1+)

Run from the repository root:
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup_v2_windows.ps1

Optional:
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup_v2_windows.ps1 -Launch

This script never prints credential values and never writes Production data.
#>
[CmdletBinding()]
param(
    [switch]$Launch
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {}

function Stop-WithError {
    param([string]$Message)
    Write-Host "[ERROR] $Message" -ForegroundColor Red
    exit 1
}

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "== $Message ==" -ForegroundColor Cyan
}

if ($env:OS -ne "Windows_NT") {
    Stop-WithError "このセットアップはWindows用です。"
}

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RepoRoot

Write-Step "Repository確認"
if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot ".git") -PathType Container)) {
    Stop-WithError "このファイルをGit clone済みのclinic-lead-systemルートで実行してください。"
}
if (-not (Get-Command git.exe -ErrorAction SilentlyContinue)) {
    Stop-WithError "Gitが見つかりません。先にGit for Windowsをインストールしてください。"
}
git --version | Out-Host

Write-Step "Python 3.12確認"
$PythonCommand = $null
$PythonArgs = @()

if (Get-Command py.exe -ErrorAction SilentlyContinue) {
    & py.exe -3.12 -c "import sys; assert sys.version_info >= (3,12)" 2>$null
    if ($LASTEXITCODE -eq 0) {
        $PythonCommand = "py.exe"
        $PythonArgs = @("-3.12")
    }
}
if (-not $PythonCommand -and (Get-Command python.exe -ErrorAction SilentlyContinue)) {
    & python.exe -c "import sys; raise SystemExit(0 if sys.version_info >= (3,12) else 1)" 2>$null
    if ($LASTEXITCODE -eq 0) {
        $PythonCommand = "python.exe"
        $PythonArgs = @()
    }
}
if (-not $PythonCommand) {
    Stop-WithError "Python 3.12以上が見つかりません。Python 3.12をインストールしてから再実行してください。"
}

Write-Step "仮想環境作成"
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    if ($PythonArgs.Count -gt 0) {
        & $PythonCommand @PythonArgs -m venv .venv
    } else {
        & $PythonCommand -m venv .venv
    }
    if ($LASTEXITCODE -ne 0) {
        Stop-WithError ".venvの作成に失敗しました。"
    }
} else {
    Write-Host ".venvは既に存在します。再利用します。"
}

& $VenvPython -c "import sys; print('Python:', sys.version.split()[0])"
if ($LASTEXITCODE -ne 0) {
    Stop-WithError ".venvのPythonを起動できません。"
}

Write-Step "依存関係インストール"
$LockFile = Join-Path $RepoRoot "requirements-lock.txt"
if (-not (Test-Path -LiteralPath $LockFile -PathType Leaf)) {
    Stop-WithError "requirements-lock.txtが見つかりません。"
}
& $VenvPython -m pip install -r $LockFile
if ($LASTEXITCODE -ne 0) {
    Stop-WithError "Python依存関係のインストールに失敗しました。"
}

Write-Step "依存関係確認"
& $VenvPython -c "import streamlit,pandas,openpyxl,yaml,requests,bs4,rapidfuzz,filelock,tzdata,psycopg; print('Dependencies: OK')"
if ($LASTEXITCODE -ne 0) {
    Stop-WithError "必要なPython依存関係が不足しています。"
}

Write-Step "Supabase Runtime設定確認"
$CredentialFile = Join-Path $RepoRoot ".supabase-runtime.env.local"
if (-not (Test-Path -LiteralPath $CredentialFile -PathType Leaf)) {
    Write-Host ""
    Write-Host "Supabase credentialファイルがありません。" -ForegroundColor Yellow
    Write-Host "既存PCから次のファイルを安全な方法でコピーしてください:"
    Write-Host "  .supabase-runtime.env.local"
    Write-Host ""
    Write-Host "コピー先:"
    Write-Host "  $CredentialFile"
    Write-Host ""
    Stop-WithError "credential配置後にこのスクリプトを再実行してください。"
}

# Validate tracked routing + private credential + optional email config without echoing secrets.
& $VenvPython -c "from pathlib import Path; from scripts.launch_v2 import load_runtime_env; load_runtime_env(Path.cwd()); print('Runtime config: OK')"
if ($LASTEXITCODE -ne 0) {
    Stop-WithError "Runtime設定を読み込めません。.supabase-runtime.env.local / config\production_runtime.envを確認してください。"
}

$EmailFile = Join-Path $RepoRoot "config\email_research.env.local"
if (Test-Path -LiteralPath $EmailFile -PathType Leaf) {
    Write-Host "Email Research config: detected"
} else {
    Write-Host "Email Research config: not found (基本機能は起動できます)" -ForegroundColor Yellow
    Write-Host "Email ResearchもこのPCで使う場合は、既存PCから次を安全にコピーしてください:"
    Write-Host "  config\email_research.env.local"
}

Write-Step "セットアップ確認"
& $VenvPython -c "from pathlib import Path; from scripts.launch_v2 import load_runtime_env; assert load_runtime_env(Path.cwd()); print('Clinic Lead System setup: OK')"
if ($LASTEXITCODE -ne 0) {
    Stop-WithError "最終設定確認に失敗しました。"
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host " Windows初回セットアップ完了" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host "次回以降は start_v2_windows.bat をダブルクリックしてください。"
Write-Host "起動時に main -> git pull --ff-only -> 最新版起動 が自動実行されます。"

if ($Launch) {
    Write-Step "最新版へ更新して起動"
    $Updater = Join-Path $RepoRoot "scripts\update_and_launch_windows.ps1"
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Updater
    exit $LASTEXITCODE
}

exit 0
