<#
Windows one-click preflight for Clinic Lead.

Fail-closed checks performed before update_and_launch_windows.ps1:
- No existing Clinic Lead Streamlit/launch_v2.py process is running.
- Production DB and Treatment sidecar exist outside OneDrive.
- The repo .venv Python can open both SQLite files with normal mode=ro.
- Production DB passes COUNT(*) and PRAGMA integrity_check.

No process is killed and no database is modified by this script.
#>

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {}

function Stop-Preflight {
    param([string]$Message)
    Write-Host ""
    Write-Host "[PREFLIGHT ERROR]" -ForegroundColor Red
    Write-Host $Message -ForegroundColor Red
    Write-Host ""
    Write-Host "DBやプロセスを自動修復・自動終了せず、安全のため起動を停止しました。" -ForegroundColor Red
    exit 1
}

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $PythonExe)) {
    Stop-Preflight "仮想環境が見つかりません: $PythonExe"
}

# Existing Clinic Lead processes can create SQLite contention and stale UI state.
# Detect only Clinic Lead command lines; never kill arbitrary Python processes.
try {
    $running = Get-CimInstance Win32_Process | Where-Object {
        $_.CommandLine -and (
            ($_.CommandLine -match 'streamlit.+app_v2\.py') -or
            ($_.CommandLine -match 'scripts[\\/]launch_v2\.py')
        )
    }
} catch {
    Stop-Preflight "実行中プロセスを確認できませんでした: $($_.Exception.Message)"
}

if ($running) {
    $details = ($running | ForEach-Object { "PID=$($_.ProcessId)  $($_.CommandLine)" }) -join "`n"
    Stop-Preflight ("Clinic Leadが既に起動中です。二重起動を防ぐため停止します。`n" +
        "既存のClinic Lead画面/黒い起動ウィンドウを閉じてから再実行してください。`n`n" + $details)
}

if (-not $env:CLINIC_DB_PATH) {
    $env:CLINIC_DB_PATH = Join-Path $HOME "CrestixData\clinic-lead\clinics.sqlite3"
}
if (-not $env:TREATMENT_RESEARCH_DB_PATH) {
    $env:TREATMENT_RESEARCH_DB_PATH = Join-Path $HOME "CrestixData\clinic-lead\treatment_research_final.sqlite3"
}

$ClinicDbPath = [System.IO.Path]::GetFullPath($env:CLINIC_DB_PATH)
$SidecarDbPath = [System.IO.Path]::GetFullPath($env:TREATMENT_RESEARCH_DB_PATH)

foreach ($path in @($ClinicDbPath, $SidecarDbPath)) {
    if (-not (Test-Path $path)) {
        Stop-Preflight "DBが見つかりません: $path"
    }
    if ($path -match '[\\/]OneDrive[\\/]') {
        Stop-Preflight ("SQLite DBをOneDrive配下へ置く運用は禁止です: $path`n" +
            "DBは $HOME\CrestixData\clinic-lead 配下へ配置してください。")
    }
}

$checkCode = @'
import json
import sqlite3
import sys

clinic_path, sidecar_path = sys.argv[1], sys.argv[2]
out = {}

def normal_ro(path):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    db.execute("PRAGMA query_only=ON")
    return db

try:
    db = normal_ro(clinic_path)
    try:
        out["clinics_count"] = db.execute("SELECT COUNT(*) FROM clinics").fetchone()[0]
        out["clinics_integrity"] = db.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        db.close()
except Exception as exc:
    out["clinics_error"] = str(exc)
    try:
        imm = sqlite3.connect(f"file:{clinic_path}?mode=ro&immutable=1", uri=True, timeout=10)
        try:
            out["clinics_immutable_count"] = imm.execute("SELECT COUNT(*) FROM clinics").fetchone()[0]
            out["clinics_immutable_integrity"] = imm.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            imm.close()
    except Exception as imm_exc:
        out["clinics_immutable_error"] = str(imm_exc)

try:
    db2 = normal_ro(sidecar_path)
    try:
        out["sidecar_integrity"] = db2.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        db2.close()
except Exception as exc:
    out["sidecar_error"] = str(exc)

print(json.dumps(out, ensure_ascii=False))
'@

$tmp = [System.IO.Path]::GetTempFileName()
Set-Content -Path $tmp -Value $checkCode -Encoding UTF8
try {
    $json = & $PythonExe $tmp "$ClinicDbPath" "$SidecarDbPath"
} finally {
    Remove-Item -Path $tmp -ErrorAction SilentlyContinue
}

if ($LASTEXITCODE -ne 0 -or -not $json) {
    Stop-Preflight "SQLite事前確認スクリプトを実行できませんでした。"
}

$result = $json | ConvertFrom-Json

if ($result.PSObject.Properties.Name -contains "clinics_error") {
    $extra = ""
    if ($result.PSObject.Properties.Name -contains "clinics_immutable_count") {
        $extra = ("`nimmutable readでは count=$($result.clinics_immutable_count), " +
            "integrity=$($result.clinics_immutable_integrity) まで読めています。" +
            "`nDB本体の破損ではなく、通常SQLite接続/ロック状態の問題が疑われます。")
    }
    Stop-Preflight ("Production DBを通常のread-only接続で開けません: $($result.clinics_error)" + $extra +
        "`nこの状態ではアプリを起動しません。")
}

if ($result.clinics_integrity -ne "ok") {
    Stop-Preflight "Production DB integrity_checkがokではありません: $($result.clinics_integrity)"
}

if ($result.PSObject.Properties.Name -contains "sidecar_error") {
    Stop-Preflight "Treatment sidecarを通常のread-only接続で開けません: $($result.sidecar_error)"
}
if ($result.sidecar_integrity -ne "ok") {
    Stop-Preflight "Treatment sidecar integrity_checkがokではありません: $($result.sidecar_integrity)"
}

Write-Host "[PREFLIGHT OK] Clinic Lead多重起動なし" -ForegroundColor Green
Write-Host "[PREFLIGHT OK] Production DB normal read-only / integrity ok ($($result.clinics_count) clinics)" -ForegroundColor Green
Write-Host "[PREFLIGHT OK] Treatment sidecar normal read-only / integrity ok" -ForegroundColor Green
exit 0
