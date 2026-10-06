<#
.SYNOPSIS
    Windows利用端末用：ローカルに既に存在するコードをそのまま起動する（オフライン・緊急時用）。

.DESCRIPTION
    scripts\update_and_launch_windows.ps1 とは役割が異なる：
      - update_and_launch_windows.ps1 = GitHub上の最新コードであることを保証してから起動する
        （dirty working tree / fetch失敗 / pull失敗のいずれでも起動せず停止する）。
      - このスクリプト（launch_v2_windows.ps1） = GitHubへの接続確認やpullは一切行わず、
        今ローカルに存在するコードをそのまま起動する。オフライン時や緊急時に使う。

    Production DBに関する安全要件はこのスクリプトでも同じように維持する（起動経路によってDBの
    安全性が変わってはならないため）：
      - DBは絶対に自動取得・自動上書きしない（読み取り専用でのみ確認する）
      - Production DB / Treatment sidecar / HP Research Batch sidecarのいずれかが
        見つからない場合は起動せず停止する
      - config/production_data_version.json の期待値と実際の行数が一致しない場合は、
        DB移行が必要である旨を表示して起動せず停止する
      - 両DBの PRAGMA integrity_check が ok 以外の場合は起動せず停止する

.PARAMETER Headless
    ブラウザーを自動起動せずアプリを起動する（scripts\launch_v2.py --headless に渡す）。

.EXAMPLE
    .\scripts\launch_v2_windows.ps1
#>

[CmdletBinding()]
param(
    [switch]$Headless
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# Windows PowerShell 5.1はファイルのBOM有無に関わらずコンソール出力をシステムの既定
# コードページで扱うことがあり、日本語メッセージが文字化けする場合がある。表示崩れは
# 処理の安全性には影響しないが、エラーメッセージが読めないと困るため明示的にUTF-8へ合わせる。
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {
    # コンソールが無い（リダイレクト実行等）場合はここで失敗してもよい -- 処理は続行する。
}

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "== $Message ==" -ForegroundColor Cyan
}

function Write-Info {
    param([string]$Message)
    Write-Host $Message
}

function Stop-WithError {
    param([string]$Message)
    Write-Host ""
    Write-Host "[ERROR]" -ForegroundColor Red
    Write-Host $Message -ForegroundColor Red
    Write-Host ""
    Write-Host "安全のため処理を停止しました。アプリは起動していません。" -ForegroundColor Red
    exit 1
}

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

Write-Step "オフラインモード：GitHubへの接続確認・pullは行いません"
$offlineCommit = $null
try {
    $offlineCommit = git rev-parse --short HEAD 2>$null
} catch {
    # git が無い/リポジトリでない環境でもローカルコードはそのまま起動できるため、ここでは停止しない。
}
if ($offlineCommit) {
    Write-Info "現在のコミット: $offlineCommit（最新版かどうかは確認していません）"
}

# Python（.venv）の確認。無ければここで停止する（アプリ起動も含め、何も自動生成しない）。
$PythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $PythonExe)) {
    Stop-WithError "仮想環境が見つかりません（$PythonExe）。先に setup_v2_windows.bat を実行してください。"
}
& $PythonExe -c "import streamlit,pandas,openpyxl,yaml,requests,bs4,rapidfuzz,filelock,tzdata" 2>$null
if ($LASTEXITCODE -ne 0) {
    Stop-WithError "必要なPython依存関係が不足しています。setup_v2_windows.bat を再実行してください。"
}

# ---------------------------------------------------------------------------
# DB確認（Production DB / Treatment sidecar / HP Research Batch sidecarの
# パス解決と存在確認のみ。
# DBそのものは一切取得・生成・上書きしない）
# ---------------------------------------------------------------------------
Write-Step "Production DBを確認しています"

if (-not $env:CLINIC_DATA_DIR) {
    $env:CLINIC_DATA_DIR = Join-Path $HOME "CrestixData\clinic-lead"
}
if (-not $env:CLINIC_DB_PATH) {
    $env:CLINIC_DB_PATH = Join-Path $env:CLINIC_DATA_DIR "clinics.sqlite3"
}
$ClinicDbPath = $env:CLINIC_DB_PATH

if (-not $env:TREATMENT_RESEARCH_DB_PATH) {
    $env:TREATMENT_RESEARCH_DB_PATH = Join-Path $env:CLINIC_DATA_DIR "treatment_research_final.sqlite3"
}
$SidecarDbPath = $env:TREATMENT_RESEARCH_DB_PATH
if (-not $env:HP_RESEARCH_BATCH_DB_PATH) {
    $env:HP_RESEARCH_BATCH_DB_PATH = Join-Path $env:CLINIC_DATA_DIR "hp_abc_batch_sidecar.sqlite3"
}
$HpBatchDbPath = $env:HP_RESEARCH_BATCH_DB_PATH

Write-Info "Production DB: $ClinicDbPath"
Write-Info "研究結果サイドカー: $SidecarDbPath"
Write-Info "HP Research Batchサイドカー: $HpBatchDbPath"

$missingDbs = @()
if (-not (Test-Path -LiteralPath $ClinicDbPath -PathType Leaf)) {
    $missingDbs += "clinics.sqlite3 (Production): $ClinicDbPath"
}
if (-not (Test-Path -LiteralPath $SidecarDbPath -PathType Leaf)) {
    $missingDbs += "treatment_research_final.sqlite3: $SidecarDbPath"
}
if (-not (Test-Path -LiteralPath $HpBatchDbPath -PathType Leaf)) {
    $missingDbs += "hp_abc_batch_sidecar.sqlite3: $HpBatchDbPath"
}
if ($missingDbs.Count -gt 0) {
    $missingDetail = ($missingDbs | ForEach-Object { "  - $_" }) -join "`n"
    Stop-WithError ("必要なDBが不足しています：`n$missingDetail`n" +
        "3DBをCrestixDataフォルダーへ配置してから再実行してください。`n" +
        "このスクリプトはDBを自動取得・自動生成・copy・migrateしません。")
}

# 行数/整合性の取得は読み取り専用（mode=ro）でのみ行う。書き込みは一切行わない。
$dbStatsCode = @'
import json
import sqlite3
import sys

clinic_path, sidecar_path, hp_batch_path = sys.argv[1], sys.argv[2], sys.argv[3]
result = {}
try:
    db = sqlite3.connect(f"file:{clinic_path}?mode=ro&immutable=1", uri=True)
    result["clinics_integrity"] = db.execute("PRAGMA integrity_check").fetchone()[0]
    result["clinics_count"] = db.execute("SELECT COUNT(*) FROM clinics").fetchone()[0]
except Exception as exc:
    result["clinics_error"] = str(exc)

try:
    db2 = sqlite3.connect(f"file:{sidecar_path}?mode=ro&immutable=1", uri=True)
    result["sidecar_integrity"] = db2.execute("PRAGMA integrity_check").fetchone()[0]
    result["treatment_rows"] = db2.execute(
        "SELECT COUNT(*) FROM clinic_treatment_research_final").fetchone()[0]
    result["treatment_clinics"] = db2.execute(
        "SELECT COUNT(DISTINCT clinic_id) FROM clinic_treatment_research_final").fetchone()[0]
    result["done"] = db2.execute(
        "SELECT COUNT(*) FROM clinic_research_status WHERE research_status='DONE'").fetchone()[0]
    result["fetch_failed"] = db2.execute(
        "SELECT COUNT(*) FROM clinic_research_status WHERE research_status='FETCH_FAILED'").fetchone()[0]
except Exception as exc:
    result["sidecar_error"] = str(exc)

try:
    db3 = sqlite3.connect(f"file:{hp_batch_path}?mode=ro&immutable=1", uri=True)
    result["hp_integrity"] = db3.execute("PRAGMA integrity_check").fetchone()[0]
    row = db3.execute("""
        SELECT COUNT(*), SUM(fetch_status='OK'), SUM(fetch_status<>'OK'),
               SUM(fetch_status='OK' AND json_array_length(treatment_categories)>0),
               SUM(fetch_status='OK' AND json_array_length(treatment_categories)=0)
        FROM hp_research_batch_results
    """).fetchone()
    (result["hp_batch_clinics"], result["hp_researched"], result["hp_failed"],
     result["hp_treatment_detected"], result["hp_treatment_not_detected"]) = row
except Exception as exc:
    result["hp_error"] = str(exc)

print(json.dumps(result, ensure_ascii=False))
'@

# python -c "<複数行・ダブルクオート混在のコード>" のように直接ネイティブコマンドの引数として
# 渡さない -- Windowsのコマンドライン引数組み立ては埋め込まれたダブルクオートのエスケープが
# 不安定になりやすいため、一意な一時ファイルに書き出してから実行する（衝突を避けるため固定の
# ファイル名は使わず GetTempFileName を使う。必ず finally で削除する）。
$dbStatsScript = [System.IO.Path]::GetTempFileName()
Set-Content -Path $dbStatsScript -Value $dbStatsCode -Encoding UTF8

try {
    $dbStatsJson = & $PythonExe $dbStatsScript "$ClinicDbPath" "$SidecarDbPath" "$HpBatchDbPath"
} finally {
    Remove-Item -Path $dbStatsScript -ErrorAction SilentlyContinue
}
if ($LASTEXITCODE -ne 0 -or -not $dbStatsJson) {
    Stop-WithError "Production DB / サイドカーの読み取りに失敗しました。"
}
$dbStats = $dbStatsJson | ConvertFrom-Json

if ($dbStats.PSObject.Properties.Name -contains "clinics_error") {
    Stop-WithError "Production DBを開けませんでした: $($dbStats.clinics_error)"
}
if ($dbStats.PSObject.Properties.Name -contains "sidecar_error") {
    Stop-WithError "研究結果サイドカーDBを開けませんでした: $($dbStats.sidecar_error)"
}
if ($dbStats.PSObject.Properties.Name -contains "hp_error") {
    Stop-WithError "HP Research BatchサイドカーDBを開けませんでした: $($dbStats.hp_error)"
}

Write-Info ("Production DB: clinics={0}件" -f $dbStats.clinics_count)
Write-Info ("サイドカー: treatment_rows={0} / treatment_clinics={1} / DONE={2} / FETCH_FAILED={3}" -f `
    $dbStats.treatment_rows, $dbStats.treatment_clinics, $dbStats.done, $dbStats.fetch_failed)
Write-Info ("HP Batch: total={0} / 完了={1} / 失敗={2} / 治療カテゴリあり={3} / なし={4}" -f `
    $dbStats.hp_batch_clinics, $dbStats.hp_researched, $dbStats.hp_failed, `
    $dbStats.hp_treatment_detected, $dbStats.hp_treatment_not_detected)

# ---------------------------------------------------------------------------
# DBバージョン確認（config/production_data_version.json の期待値との完全一致確認）
# ---------------------------------------------------------------------------
Write-Step "DBバージョンを確認しています"

$VersionJsonPath = Join-Path $RepoRoot "config\production_data_version.json"
if (-not (Test-Path $VersionJsonPath)) {
    Stop-WithError "config\production_data_version.json が見つかりません。"
}
$expected = Get-Content $VersionJsonPath -Raw | ConvertFrom-Json

Write-Info ("コードが前提とするDBバージョン: {0}（更新日 {1}）" -f $expected.data_version, $expected.updated_at)

$mismatches = @()
if ($dbStats.clinics_count -ne $expected.clinics_count) {
    $mismatches += "clinics_count: 期待値={0} 実際={1}" -f $expected.clinics_count, $dbStats.clinics_count
}
if ($dbStats.treatment_clinics -ne $expected.treatment_clinics) {
    $mismatches += "treatment_clinics: 期待値={0} 実際={1}" -f $expected.treatment_clinics, $dbStats.treatment_clinics
}
if ($dbStats.treatment_rows -ne $expected.treatment_rows) {
    $mismatches += "treatment_rows: 期待値={0} 実際={1}" -f $expected.treatment_rows, $dbStats.treatment_rows
}
if ($dbStats.done -ne $expected.done) {
    $mismatches += "done: 期待値={0} 実際={1}" -f $expected.done, $dbStats.done
}
if ($dbStats.fetch_failed -ne $expected.fetch_failed) {
    $mismatches += "fetch_failed: 期待値={0} 実際={1}" -f $expected.fetch_failed, $dbStats.fetch_failed
}
foreach ($field in @("hp_batch_clinics", "hp_researched", "hp_failed", "hp_treatment_detected", "hp_treatment_not_detected")) {
    if ($dbStats.$field -ne $expected.$field) {
        $mismatches += "${field}: 期待値=$($expected.$field) 実際=$($dbStats.$field)"
    }
}
if ($dbStats.hp_batch_clinics -ne ($dbStats.hp_researched + $dbStats.hp_failed)) {
    $mismatches += "HP Batch invariant: total != researched + failed"
}
if ($dbStats.hp_researched -ne ($dbStats.hp_treatment_detected + $dbStats.hp_treatment_not_detected)) {
    $mismatches += "HP Batch invariant: researched != treatment detected + not detected"
}

if ($mismatches.Count -gt 0) {
    $detail = ($mismatches | ForEach-Object { "  - $_" }) -join "`n"
    Stop-WithError (
        "DBバージョン不一致を検出しました。最新版DBへの移行が必要です。`n$detail`n`n" +
        "このスクリプトはDBを自動取得・自動上書きしません。以下の手動運用でDBを移行してください：`n" +
        "  1. Mac側で最新のCrestixDataに対し sqlite3 の .backup でバックアップを作成する`n" +
        "  2. バックアップをZIPで圧縮する`n" +
        "  3. Windows側のCrestixDataフォルダーへ展開・配置する`n" +
        "DB移行後、本スクリプトを再実行してください。"
    )
}

Write-Info "DBバージョンは一致しています。"

# ---------------------------------------------------------------------------
# integrity check（ok以外ならSTOP。アプリは起動しない）
# ---------------------------------------------------------------------------
Write-Step "integrity checkを実行しています"

if ($dbStats.clinics_integrity -ne "ok") {
    Stop-WithError "Production DBのintegrity_checkがokではありません: $($dbStats.clinics_integrity)"
}
if ($dbStats.sidecar_integrity -ne "ok") {
    Stop-WithError "研究結果サイドカーDBのintegrity_checkがokではありません: $($dbStats.sidecar_integrity)"
}
if ($dbStats.hp_integrity -ne "ok") {
    Stop-WithError "HP Research BatchサイドカーDBのintegrity_checkがokではありません: $($dbStats.hp_integrity)"
}
Write-Info "Production DB / Treatmentサイドカー / HP Batchサイドカーすべてintegrity_check: ok"

# ---------------------------------------------------------------------------
# アプリ起動
# ---------------------------------------------------------------------------
Write-Step "アプリを起動しています"

$launchArgs = @(Join-Path $RepoRoot "scripts\launch_v2.py")
if ($Headless) {
    $launchArgs += "--headless"
}

Write-Info "終了するまでこのウィンドウを閉じないでください。終了はCtrl+Cです。"
& $PythonExe @launchArgs
exit $LASTEXITCODE
