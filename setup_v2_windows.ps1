<#
Clinic Lead System - Windows first-time setup (PowerShell 5.1+)

Run from the repository root:
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup_v2_windows.ps1

Optional:
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup_v2_windows.ps1 -Launch

On success it creates the Desktop shortcut "Clinic Lead System", so later launches
need only a double-click (no PowerShell / Git operation by the user).

This script never prints credential values and never writes Production data.
This file is ASCII-only on purpose (Windows PowerShell 5.1 encoding safety).
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
    Stop-WithError "This setup is for Windows only."
}

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RepoRoot

Write-Step "Repository check"
if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot ".git") -PathType Container)) {
    Stop-WithError "Run this file from the root of the git-cloned clinic-lead-system repository."
}
if (-not (Get-Command git.exe -ErrorAction SilentlyContinue)) {
    Stop-WithError "Git was not found. Install Git for Windows first."
}
git --version | Out-Host

Write-Step "Python 3.12 check"
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
    Stop-WithError "Python 3.12 or later was not found. Install Python 3.12 and run this again."
}

Write-Step "Create virtual environment"
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    if ($PythonArgs.Count -gt 0) {
        & $PythonCommand @PythonArgs -m venv .venv
    } else {
        & $PythonCommand -m venv .venv
    }
    if ($LASTEXITCODE -ne 0) {
        Stop-WithError "Failed to create .venv."
    }
} else {
    Write-Host ".venv already exists. Reusing it."
}

& $VenvPython -c "import sys; print('Python:', sys.version.split()[0])"
if ($LASTEXITCODE -ne 0) {
    Stop-WithError "Python in .venv cannot be started."
}

Write-Step "Install dependencies"
$LockFile = Join-Path $RepoRoot "requirements-lock.txt"
if (-not (Test-Path -LiteralPath $LockFile -PathType Leaf)) {
    Stop-WithError "requirements-lock.txt was not found."
}
& $VenvPython -m pip install -r $LockFile
if ($LASTEXITCODE -ne 0) {
    Stop-WithError "Failed to install Python dependencies."
}

Write-Step "Dependency check"
& $VenvPython -c "import streamlit,pandas,openpyxl,yaml,requests,bs4,rapidfuzz,filelock,tzdata,psycopg; print('Dependencies: OK')"
if ($LASTEXITCODE -ne 0) {
    Stop-WithError "Required Python dependencies are missing."
}

Write-Step "Supabase Runtime config check"
$CredentialFile = Join-Path $RepoRoot ".supabase-runtime.env.local"
if (-not (Test-Path -LiteralPath $CredentialFile -PathType Leaf)) {
    Write-Host ""
    Write-Host "Supabase credential file is missing." -ForegroundColor Yellow
    Write-Host "Copy this file from an existing PC by a secure method:"
    Write-Host "  .supabase-runtime.env.local"
    Write-Host ""
    Write-Host "Destination:"
    Write-Host "  $CredentialFile"
    Write-Host ""
    Stop-WithError "Place the credential file and run this script again."
}

# Validate tracked routing + private credential + optional email config without echoing secrets.
& $VenvPython -c "from pathlib import Path; from scripts.launch_v2 import load_runtime_env; load_runtime_env(Path.cwd()); print('Runtime config: OK')"
if ($LASTEXITCODE -ne 0) {
    Stop-WithError "Runtime config cannot be loaded. Check .supabase-runtime.env.local / config\production_runtime.env."
}

$EmailFile = Join-Path $RepoRoot "config\email_research.env.local"
if (Test-Path -LiteralPath $EmailFile -PathType Leaf) {
    Write-Host "Email Research config: detected"
} else {
    Write-Host "Email Research config: not found (the main app still starts; Email Research is unavailable)" -ForegroundColor Yellow
    Write-Host "To use Email Research on this PC, securely copy this file from an existing PC:"
    Write-Host "  config\email_research.env.local"
}

Write-Step "Setup verification"
& $VenvPython -c "from pathlib import Path; from scripts.launch_v2 import load_runtime_env; assert load_runtime_env(Path.cwd()); print('Clinic Lead System setup: OK')"
if ($LASTEXITCODE -ne 0) {
    Stop-WithError "Final setup verification failed."
}

Write-Step "Desktop shortcut"
$ShortcutScript = Join-Path $RepoRoot "scripts\create_windows_desktop_shortcut.ps1"
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $ShortcutScript
if ($LASTEXITCODE -ne 0) {
    Stop-WithError "Setup finished, but the Desktop shortcut could not be created. Re-run: scripts\create_windows_desktop_shortcut.ps1"
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host " Windows first-time setup complete" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host "From now on, double-click 'Clinic Lead System' on the Desktop."
Write-Host "Each launch runs: git switch main -> git pull --ff-only -> start the latest version."

if ($Launch) {
    Write-Step "Update to latest and launch"
    $Updater = Join-Path $RepoRoot "scripts\update_and_launch_windows.ps1"
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Updater
    exit $LASTEXITCODE
}

exit 0
