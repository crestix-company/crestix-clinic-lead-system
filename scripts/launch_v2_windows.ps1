[CmdletBinding()]
param([switch]$Headless,[switch]$AcceptanceProbe,[string]$AcceptanceToken = "")
$Target = Join-Path $PSScriptRoot "windows\launch_v2_windows.ps1"
$Forward = @()
if ($Headless) { $Forward += "-Headless" }
if ($AcceptanceProbe) { $Forward += "-AcceptanceProbe"; $Forward += @("-AcceptanceToken", $AcceptanceToken) }
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Target @Forward
exit $LASTEXITCODE
