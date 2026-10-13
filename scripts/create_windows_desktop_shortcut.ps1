<#
Clinic Lead System - create or update the Windows Desktop shortcut (PowerShell 5.1+)

Creates "<Desktop>\Clinic Lead System.lnk" that runs start_v2_windows.bat from the
repository root. The Desktop folder is resolved with
[Environment]::GetFolderPath("Desktop"), so OneDrive-redirected Desktops work.

Usage:
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\create_windows_desktop_shortcut.ps1

The shortcut carries no arguments: credentials and DB URLs are never written to it.
This file is ASCII-only on purpose (Windows PowerShell 5.1 encoding safety).
#>
[CmdletBinding()]
param(
    # Test/override hook only. Default is the real Desktop folder of the current user.
    [string]$DesktopPath = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {}

function Stop-WithError {
    param([string]$Message)
    Write-Host "[ERROR] $Message" -ForegroundColor Red
    exit 1
}

$ShortcutName = "Clinic Lead System"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$TargetPath = Join-Path $RepoRoot "start_v2_windows.bat"

if (-not (Test-Path -LiteralPath $TargetPath -PathType Leaf)) {
    Stop-WithError "start_v2_windows.bat was not found in the repository root: $RepoRoot"
}

if (-not $DesktopPath) {
    $DesktopPath = [Environment]::GetFolderPath("Desktop")
}
if (-not $DesktopPath -or -not (Test-Path -LiteralPath $DesktopPath -PathType Container)) {
    Stop-WithError "Desktop folder could not be resolved."
}

$ShortcutPath = Join-Path $DesktopPath ($ShortcutName + ".lnk")
if (Test-Path -LiteralPath $ShortcutPath -PathType Container) {
    Stop-WithError "A folder with the shortcut name already exists: $ShortcutPath"
}

$Shell = New-Object -ComObject WScript.Shell
try {
    # CreateShortcut loads an existing .lnk if present; every field is overwritten below,
    # so re-running this script updates the shortcut in place.
    $Shortcut = $Shell.CreateShortcut($ShortcutPath)
    $Shortcut.TargetPath = $TargetPath
    $Shortcut.Arguments = ""
    $Shortcut.WorkingDirectory = $RepoRoot
    $Shortcut.Description = "Update to the latest main and start Clinic Lead System"
    $Shortcut.IconLocation = "$env:SystemRoot\System32\shell32.dll,13"
    $Shortcut.WindowStyle = 1
    $Shortcut.Save()
} finally {
    [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($Shell)
}

if (-not (Test-Path -LiteralPath $ShortcutPath -PathType Leaf)) {
    Stop-WithError "Failed to save the Desktop shortcut."
}

Write-Host "Desktop shortcut ready: $ShortcutPath"
Write-Host "  Target          : $TargetPath"
Write-Host "  WorkingDirectory: $RepoRoot"
exit 0
