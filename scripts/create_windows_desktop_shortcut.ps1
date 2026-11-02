[CmdletBinding()]
param([string]$DesktopPath = "")
$Target = Join-Path $PSScriptRoot "windows\create_windows_desktop_shortcut.ps1"
if ($DesktopPath) { & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Target -DesktopPath $DesktopPath } else { & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Target }
exit $LASTEXITCODE
