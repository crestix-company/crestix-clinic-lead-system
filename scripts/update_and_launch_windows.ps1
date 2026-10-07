<#
.SYNOPSIS
    Windows利用端末用：「GitHub上の最新コード + 正しいProduction DB」であることを保証してから
    アプリを起動する。保証できない場合は必ず起動せずに停止する。

.DESCRIPTION
    運用：Mac=開発機、GitHub=コードのSource of Truth、CrestixData(SQLite)=データのSource of Truth、
    Windows=利用端末。

    このスクリプトの役割は「保証」であり、「できる範囲で動かす」ではない。以下のいずれかに
    当てはまる場合、アプリを起動せず必ず停止する（自動修復・自動上書きは一切行わない）：
      - ローカルに未コミットの変更がある（dirty working tree）
      - GitHubへの fetch に失敗した（オフライン・接続不可等）
      - git pull --ff-only に失敗した（履歴が分岐している等）
      - Production DB / 研究結果サイドカーDBが見つからない
      - DBの実際の行数が config/production_data_version.json の期待値と一致しない
      - PRAGMA integrity_check が ok 以外を返した

    GitHubへ接続できないオフライン時や緊急時に、ローカルに既にあるコードをそのまま起動したい
    場合は、このスクリプトではなく scripts\launch_v2_windows.ps1 を使うこと（そちらはgitの
    fetch/pullを一切行わない）。

    Supabase WRITE切替設定はgit-ignore済み .supabase-runtime.env.localからPython launcherが
    読み込む。SUPABASE_RUNTIME_DB_URLの値はコンソールへ表示しない。

    Production DBは絶対に自動取得・自動上書きしない。DBの移行は、Mac側でSQLiteの`.backup`を
    取り、ZIPでWindowsのCrestixDataフォルダーへ手動で配置する運用を継続する。このスクリプトは
    その手動運用を前提に、「今のWindows実DBが今のコードが期待するDBバージョンと一致しているか」
    を読み取り専用で確認するだけで、DBそのものには一切書き込まない。

    手順：
      1. git status --porcelain が何か返せば即停止（dirty working tree）。
      2. git fetch origin が失敗したら即停止。
      3. git pull --ff-only が失敗したら即停止（履歴の自動修正は行わない）。
      4. Production DB / 研究結果サイドカーDBの存在と読み取り可否を確認する。
      5. config/production_data_version.json の期待値と実際の行数を比較する
         （完全一致が必要。1件でも違えば停止 -- DB移行が必要）。
      6. 両DBに対して PRAGMA integrity_check を実行する（ok以外なら停止）。
      7. すべて確認できたら .venv の Python で scripts\launch_v2.py を起動する。

.PARAMETER Headless
    ブラウザーを自動起動せずアプリを起動する（scripts\launch_v2.py --headless に渡す）。

.EXAMPLE
    .\scripts\update_and_launch_windows.ps1
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

# ---------------------------------------------------------------------------
# 1/6 dirty working tree チェック（未コミットの変更が1件でもあれば即停止）
# ---------------------------------------------------------------------------
Write-Step "1/6 ローカルの変更状態を確認しています"

if (-not (Test-Path (Join-Path $RepoRoot ".git"))) {
    Stop-WithError ("このフォルダーはgitリポジトリではありません。`n" +
        "GitHub上の最新コードであることを保証できないため停止しました。`n" +
        "ローカルのコードをそのまま起動したい場合は .\scripts\launch_v2_windows.ps1 を使用してください。")
}

$gitStatus = $null
try {
    $gitStatus = git status --porcelain 2>&1
} catch {
    Stop-WithError "git コマンドを実行できませんでした（$($_.Exception.Message)）。Gitがインストールされているか確認してください。"
}
if ($LASTEXITCODE -ne 0) {
    Stop-WithError "git status の実行に失敗しました。`n$gitStatus"
}
if ($gitStatus) {
    Write-Host ""
    Write-Host "変更内容（git status --porcelain）:" -ForegroundColor Yellow
    Write-Host $gitStatus
    Stop-WithError ("Windows側に未コミットの変更があります。`n" +
        "安全のため自動更新・起動を停止しました。`n" +
        "変更内容を確認し、不要であれば手元で個別に取り消すか、必要であれば管理者に相談してください。`n" +
        "このスクリプトはローカル変更を自動では削除しません（reset --hard / checkout -- . / clean -fd / stash は一切行いません）。")
}
Write-Info "未コミットの変更はありません。"

try {
    git switch main 2>&1 | Out-Host
} catch {
    Stop-WithError "main branchへ切り替えられませんでした（$($_.Exception.Message)）。"
}
if ($LASTEXITCODE -ne 0) {
    Stop-WithError "main branchへ切り替えられませんでした。ローカル状態を管理者へ共有してください。"
}
Write-Info "main branchを使用します。"

# ---------------------------------------------------------------------------
# 2/6 git fetch（失敗したら即停止。オフライン運用は launch_v2_windows.ps1 を使う）
# ---------------------------------------------------------------------------
Write-Step "2/6 GitHubから最新版を確認しています"

try {
    git fetch origin main 2>&1 | Out-Host
} catch {
    Stop-WithError "git コマンドを実行できませんでした（$($_.Exception.Message)）。"
}
if ($LASTEXITCODE -ne 0) {
    Stop-WithError ("GitHubから最新版を確認できませんでした。`n" +
        "ネットワーク接続またはGitHub接続を確認してください。`n`n" +
        "オフラインで既存コードを起動したい場合は、`n" +
        ".\scripts\launch_v2_windows.ps1`n" +
        "を使用してください。")
}
Write-Info "GitHubから最新情報を取得しました。"

# ---------------------------------------------------------------------------
# 3/6 git pull --ff-only（失敗したら即停止。履歴の自動修正は行わない）
# ---------------------------------------------------------------------------
Write-Step "3/6 コードを最新版に更新しています"

try {
    git pull --ff-only origin main 2>&1 | Out-Host
} catch {
    Stop-WithError "git コマンドを実行できませんでした（$($_.Exception.Message)）。"
}
if ($LASTEXITCODE -ne 0) {
    Stop-WithError ("git pull --ff-only に失敗しました（履歴が分岐している可能性があります）。`n" +
        "履歴の自動修正は行いません。管理者に確認するか、新しくcloneし直してください。")
}
Write-Info "コードを最新版に更新しました。"

$currentCommit = git rev-parse --short HEAD 2>$null
if ($currentCommit) {
    Write-Info "現在のコミット: $currentCommit"
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
# 4/6 DB確認（Production DB / 研究結果サイドカーのパス解決と存在確認のみ。
#    DBそのものは一切取得・生成・上書きしない）
# ---------------------------------------------------------------------------
Write-Step "4/6 Production DBを確認しています"

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
        SELECT COUNT(*),
               SUM(fetch_status='OK'),
               SUM(fetch_status<>'OK'),
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
# 5/6 DBバージョン確認（config/production_data_version.json の期待値との完全一致確認）
# ---------------------------------------------------------------------------
Write-Step "5/6 DBバージョンを確認しています"

$VersionJsonPath = Join-Path $RepoRoot "config\production_data_version.json"
if (-not (Test-Path $VersionJsonPath)) {
    Stop-WithError "config\production_data_version.json が見つかりません。コードの更新に失敗している可能性があります。"
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

Write-Info "DBバージョンは一致しています。コードのみの更新のため、DB移行は不要です。"

# ---------------------------------------------------------------------------
# 6/6 integrity check（ok以外ならSTOP。アプリは起動しない）
# ---------------------------------------------------------------------------
Write-Step "6/6 integrity checkを実行しています"

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
