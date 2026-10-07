<# Windows PowerShell 5.1用：最新版へ安全に更新しSupabase-only Runtimeを起動する。 #>
[CmdletBinding()]
param([switch]$Headless)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; $OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
function Stop-WithError { param([string]$Message); Write-Host "[ERROR] $Message" -ForegroundColor Red; exit 1 }
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot
if (-not (Test-Path (Join-Path $RepoRoot ".git"))) { Stop-WithError "gitリポジトリではありません。" }
$Dirty = git status --porcelain 2>&1
if ($LASTEXITCODE -ne 0 -or $Dirty) { Stop-WithError "未コミット変更があるかgit statusに失敗しました。" }
git switch main 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) { Stop-WithError "main branchへ切り替えられませんでした。" }
git fetch origin main 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) { Stop-WithError "GitHubから最新版を確認できませんでした。" }
git pull --ff-only origin main 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) { Stop-WithError "fast-forward updateに失敗しました。" }
$Launcher = Join-Path $RepoRoot "scripts\launch_v2_windows.ps1"
if ($Headless) { & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Launcher -Headless } else { & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Launcher }
exit $LASTEXITCODE
