<# Windows PowerShell 5.1: launch the checked-out branch without updating it. ASCII-only. #>
[CmdletBinding()]
param([switch]$Headless)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; $OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
function Stop-WithError { param([string]$Message); Write-Host "[ERROR] $Message" -ForegroundColor Red; exit 1 }
function Invoke-Git {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { $output = & git.exe @args 2>&1 | ForEach-Object { "$_" }; $code = $LASTEXITCODE }
    finally { $ErrorActionPreference = $previous }
    return [pscustomobject]@{ ExitCode = $code; Output = @($output) }
}
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $RepoRoot
if (-not (Test-Path (Join-Path $RepoRoot ".git"))) { Stop-WithError "Not a git repository: $RepoRoot" }
if (-not (Get-Command git.exe -ErrorAction SilentlyContinue)) { Stop-WithError "Git was not found. Install Git for Windows first." }
$Status = Invoke-Git status --porcelain
if ($Status.ExitCode -ne 0) { $Status.Output | Out-Host; Stop-WithError "git status failed." }
if (@($Status.Output | Where-Object { $_ }).Count -gt 0) {
    $Status.Output | Out-Host
    Stop-WithError "Uncommitted changes found. Nothing was changed. Commit or move them, then start again."
}
$Branch = Invoke-Git branch --show-current
if ($Branch.ExitCode -ne 0 -or -not $Branch.Output.Count -or -not $Branch.Output[0]) { Stop-WithError "Could not identify the current Git branch." }
$Commit = Invoke-Git rev-parse HEAD
if ($Commit.ExitCode -ne 0 -or -not $Commit.Output.Count) { Stop-WithError "Could not identify the current Git commit." }
Write-Host "Using checked-out branch: $($Branch.Output[0])"
Write-Host "Using checked-out commit: $($Commit.Output[0])"
Write-Host "No branch switch, fetch, pull, or remote update will be performed."
$Launcher = Join-Path $RepoRoot "scripts\windows\launch_v2_windows.ps1"
if ($Headless) { & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Launcher -Headless } else { & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Launcher }
exit $LASTEXITCODE
