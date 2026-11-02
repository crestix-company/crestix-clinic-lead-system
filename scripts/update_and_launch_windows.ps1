[CmdletBinding()]
param([switch]$Headless)
$Target = Join-Path $PSScriptRoot "windows\update_and_launch_windows.ps1"
if ($Headless) { & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Target -Headless } else { & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Target }
exit $LASTEXITCODE
