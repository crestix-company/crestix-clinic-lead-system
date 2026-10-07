<# Windows PowerShell 5.1 actual-machine acceptance. No secret value is printed. #>
[CmdletBinding()]
param()
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; $OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

if ($PSVersionTable.PSVersion.Major -ne 5 -or $PSVersionTable.PSVersion.Minor -lt 1) {
    Write-Host "WINDOWS_POWERSHELL_5_1=FAIL"
    exit 1
}
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Launcher = Join-Path $RepoRoot "scripts\launch_v2_windows.ps1"
$Token = "__stage5_windows_acceptance_" + [Guid]::NewGuid().ToString("N")
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Launcher -AcceptanceProbe -AcceptanceToken $Token
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host "WINDOWS_POWERSHELL_5_1=PASS"
Write-Host "WINDOWS_ACTUAL_MACHINE_ACCEPTANCE=PASS"
