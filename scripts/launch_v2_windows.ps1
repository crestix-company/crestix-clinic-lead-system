<# Windows PowerShell 5.1用 Supabase-only launcher。SQLite DB/sidecarは使用しない。 #>
[CmdletBinding()]
param(
    [switch]$Headless,
    [switch]$AcceptanceProbe,
    [string]$AcceptanceToken = ""
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; $OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
function Stop-WithError { param([string]$Message); Write-Host "[ERROR] $Message" -ForegroundColor Red; exit 1 }
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot
$PythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) { Stop-WithError "Python仮想環境が見つかりません。" }
$CredentialFile = Join-Path $RepoRoot ".supabase-runtime.env.local"
if (-not (Test-Path -LiteralPath $CredentialFile -PathType Leaf)) { Stop-WithError "Supabase Runtime credentialが見つかりません。" }
& $PythonExe -c "import streamlit,pandas,openpyxl,yaml,requests,bs4,rapidfuzz,filelock,tzdata,psycopg" 2>$null
if ($LASTEXITCODE -ne 0) { Stop-WithError "必要なPython依存関係が不足しています。" }
Write-Host "Supabase-only構成で起動します（credential値は表示しません）。"
$AcceptanceHelper = Join-Path $RepoRoot "scripts\supabase_migration\stage5_external_acceptance.py"
if ($AcceptanceProbe) {
    if (-not $AcceptanceToken) { Stop-WithError "Acceptance tokenがありません。" }
    & $PythonExe $AcceptanceHelper windows --token $AcceptanceToken
    exit $LASTEXITCODE
}
$Launcher = Join-Path $RepoRoot "scripts\launch_v2.py"
if ($Headless) { & $PythonExe $Launcher --headless } else { & $PythonExe $Launcher }
exit $LASTEXITCODE
