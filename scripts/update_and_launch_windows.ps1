<# Windows PowerShell 5.1: safely fast-forward to the latest main, then start the Supabase-only runtime. ASCII-only. #>
[CmdletBinding()]
param([switch]$Headless)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; $OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
function Stop-WithError { param([string]$Message); Write-Host "[ERROR] $Message" -ForegroundColor Red; exit 1 }
# Git writes normal progress ("Already on 'main'", fetch info) to stderr. Under PowerShell 5.1 with
# ErrorActionPreference=Stop, "2>&1" turns those lines into terminating NativeCommandErrors, so run git
# with Continue and judge success by the exit code only.
function Invoke-Git {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { $output = & git.exe @args 2>&1 | ForEach-Object { "$_" }; $code = $LASTEXITCODE }
    finally { $ErrorActionPreference = $previous }
    return [pscustomobject]@{ ExitCode = $code; Output = @($output) }
}
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot
if (-not (Test-Path (Join-Path $RepoRoot ".git"))) { Stop-WithError "Not a git repository: $RepoRoot" }
if (-not (Get-Command git.exe -ErrorAction SilentlyContinue)) { Stop-WithError "Git was not found. Install Git for Windows first." }
$Status = Invoke-Git status --porcelain
if ($Status.ExitCode -ne 0) { $Status.Output | Out-Host; Stop-WithError "git status failed." }
if (@($Status.Output | Where-Object { $_ }).Count -gt 0) {
    $Status.Output | Out-Host
    Stop-WithError "Uncommitted changes found. Nothing was changed. Commit or move them, then start again."
}
$Result = Invoke-Git switch main
$Result.Output | Out-Host
if ($Result.ExitCode -ne 0) { Stop-WithError "Could not switch to the main branch." }
$Result = Invoke-Git fetch origin main
$Result.Output | Out-Host
if ($Result.ExitCode -ne 0) { Stop-WithError "Could not check GitHub for the latest version (git fetch origin main)." }
$Result = Invoke-Git pull --ff-only origin main
$Result.Output | Out-Host
if ($Result.ExitCode -ne 0) { Stop-WithError "Fast-forward update failed (git pull --ff-only origin main). Local history differs from GitHub main." }
$Launcher = Join-Path $RepoRoot "scripts\launch_v2_windows.ps1"
if ($Headless) { & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Launcher -Headless } else { & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Launcher }
exit $LASTEXITCODE
