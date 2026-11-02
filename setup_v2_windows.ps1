[CmdletBinding()]
param([switch]$Launch)
$Target = Join-Path $PSScriptRoot "scripts\windows\setup_v2_windows.ps1"
if ($Launch) { & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Target -Launch } else { & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Target }
exit $LASTEXITCODE
