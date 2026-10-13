import json
import os
import re
import shutil
import subprocess

import pytest

from src.utils.config import ROOT

SHORTCUT_SCRIPT = "scripts/create_windows_desktop_shortcut.ps1"
SETUP_SCRIPT = "setup_v2_windows.ps1"
WINDOWS_SCRIPTS = (
    SHORTCUT_SCRIPT,
    SETUP_SCRIPT,
    "scripts/update_and_launch_windows.ps1",
    "scripts/launch_v2_windows.ps1",
    "start_v2_windows.bat",
)
DESTRUCTIVE_GIT = (
    "git reset --hard",
    "git clean",
    "git checkout -- .",
    "git restore .",
    "git rebase",
    "git merge",
    "git stash",
)
POWERSHELL = shutil.which("powershell.exe")
windows_only = pytest.mark.skipif(
    os.name != "nt" or POWERSHELL is None, reason="requires Windows PowerShell 5.1"
)


def _text(relative):
    return (ROOT / relative).read_text(encoding="utf-8-sig")


def _run_ps(command):
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def _ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def test_new_and_changed_ascii_scripts_contain_no_non_ascii_bytes():
    for relative in (SHORTCUT_SCRIPT, SETUP_SCRIPT):
        (ROOT / relative).read_bytes().decode("ascii")


def test_shortcut_uses_known_folder_desktop_not_fixed_path():
    text = _text(SHORTCUT_SCRIPT)
    assert '[Environment]::GetFolderPath("Desktop")' in text
    assert not re.search(r"C:\\\\?Users", text, re.IGNORECASE)
    assert "$env:USERPROFILE" not in text


def test_shortcut_targets_start_bat_from_repo_root_without_arguments():
    text = _text(SHORTCUT_SCRIPT)
    assert '$ShortcutName = "Clinic Lead System"' in text
    assert '$TargetPath = Join-Path $RepoRoot "start_v2_windows.bat"' in text
    assert "$Shortcut.WorkingDirectory = $RepoRoot" in text
    assert '$Shortcut.Arguments = ""' in text
    assert "WScript.Shell" in text


def test_no_credentials_in_shortcut_or_windows_scripts():
    for relative in WINDOWS_SCRIPTS:
        text = _text(relative)
        assert "postgresql://" not in text
        assert "SUPABASE_RUNTIME_DB_URL" not in text
        assert "EMAIL_RESEARCH_DB_URL" not in text
    shortcut = _text(SHORTCUT_SCRIPT)
    assert "env.local" not in shortcut


def test_update_launcher_keeps_ff_only_and_no_destructive_git():
    update = _text("scripts/update_and_launch_windows.ps1")
    assert "Invoke-Git status --porcelain" in update
    assert "Invoke-Git switch main" in update
    assert "Invoke-Git fetch origin main" in update
    assert "Invoke-Git pull --ff-only origin main" in update
    assert "Get-Command git.exe" in update
    assert update.index("Invoke-Git status --porcelain") < update.index("Invoke-Git switch main")
    assert update.index("Invoke-Git switch main") < update.index("Invoke-Git fetch origin main")
    assert update.index("Invoke-Git fetch origin main") < update.index("Invoke-Git pull --ff-only origin main")
    for relative in WINDOWS_SCRIPTS:
        text = _text(relative).lower()
        for command in DESTRUCTIVE_GIT:
            assert command not in text, (relative, command)


def test_launch_ordering_keeps_venv_and_credential_checks_and_port_logic_in_launch_v2():
    launch = _text("scripts/launch_v2_windows.ps1")
    assert launch.index(".venv\\Scripts\\python.exe") < launch.index(".supabase-runtime.env.local")
    assert 'Join-Path $RepoRoot "scripts\\launch_v2.py"' in launch
    assert "email_research.env.local" in launch
    for relative in (SHORTCUT_SCRIPT, "scripts/update_and_launch_windows.ps1", "scripts/launch_v2_windows.ps1"):
        text = _text(relative)
        assert "Stop-Process" not in text
        assert "taskkill" not in text.lower()


def test_setup_creates_shortcut_only_after_successful_setup():
    setup = _text(SETUP_SCRIPT)
    call = 'Join-Path $RepoRoot "scripts\\create_windows_desktop_shortcut.ps1"'
    assert setup.count(call) == 1
    assert setup.index("Clinic Lead System setup: OK") < setup.index(call)
    assert setup.index(call) < setup.index("Windows first-time setup complete")


@windows_only
def test_powershell_51_parses_all_windows_scripts():
    for relative in WINDOWS_SCRIPTS:
        if not relative.endswith(".ps1"):
            continue
        path = ROOT / relative
        command = (
            "if ($PSVersionTable.PSVersion.Major -ne 5) { exit 3 }; $t=$null; $e=$null; "
            f"[void][System.Management.Automation.Language.Parser]::ParseFile({_ps_quote(path)}, [ref]$t, [ref]$e); "
            "if ($e.Count -gt 0) { $e | ForEach-Object { $_.Message }; exit 2 }; exit 0"
        )
        result = _run_ps(command)
        assert result.returncode == 0, (relative, result.stdout, result.stderr)


@windows_only
def test_desktop_folder_resolves_via_known_folder():
    result = _run_ps(
        '[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; [Environment]::GetFolderPath("Desktop")'
    )
    assert result.returncode == 0
    desktop = result.stdout.strip()
    assert desktop
    assert os.path.isdir(desktop)


@windows_only
def test_shortcut_creation_is_idempotent_on_onedrive_style_unicode_path(tmp_path):
    desktop = tmp_path / "OneDrive" / "デスクトップ"
    desktop.mkdir(parents=True)
    script = ROOT / SHORTCUT_SCRIPT
    run = (
        f"& {_ps_quote(POWERSHELL)} -NoProfile -ExecutionPolicy Bypass -File {_ps_quote(script)} "
        f"-DesktopPath {_ps_quote(desktop)}; exit $LASTEXITCODE"
    )
    for _ in range(2):
        result = _run_ps(run)
        assert result.returncode == 0, (result.stdout, result.stderr)

    links = list(desktop.iterdir())
    assert [p.name for p in links] == ["Clinic Lead System.lnk"]

    read = _run_ps(
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({_ps_quote(links[0])}); "
        "@{target=$s.TargetPath; cwd=$s.WorkingDirectory; args=$s.Arguments} | ConvertTo-Json -Compress"
    )
    assert read.returncode == 0, read.stderr
    info = json.loads(read.stdout)
    assert os.path.normcase(info["target"]) == os.path.normcase(str(ROOT / "start_v2_windows.bat"))
    assert os.path.normcase(info["cwd"].rstrip("\\")) == os.path.normcase(str(ROOT))
    assert info["args"] == ""


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _updater_sandbox(tmp_path):
    """Clone of a local origin containing the real updater and a stub launcher that only reports."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    _git(tmp_path, "init", "-b", "main", str(seed))
    (seed / "scripts").mkdir()
    shutil.copy(ROOT / "scripts/update_and_launch_windows.ps1", seed / "scripts")
    (seed / "scripts/launch_v2_windows.ps1").write_text('Write-Host "STUB_LAUNCHED"; exit 0\n', encoding="ascii")
    for args in (("config", "user.email", "t@example.com"), ("config", "user.name", "t"), ("add", "."),
                 ("commit", "-m", "seed"), ("remote", "add", "origin", str(origin)), ("push", "origin", "main")):
        _git(seed, *args)
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(origin), str(clone))
    return seed, clone


def _run_updater(clone):
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(clone / "scripts/update_and_launch_windows.ps1")],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )


@windows_only
def test_updater_survives_git_stderr_on_powershell_51_and_fast_forwards(tmp_path):
    seed, clone = _updater_sandbox(tmp_path)
    (seed / "NEW.txt").write_text("x", encoding="ascii")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "new")
    _git(seed, "push", "origin", "main")
    result = _run_updater(clone)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "NativeCommandError" not in result.stdout + result.stderr
    assert "STUB_LAUNCHED" in result.stdout
    assert (clone / "NEW.txt").exists()


@windows_only
def test_updater_stops_without_touching_uncommitted_changes(tmp_path):
    _, clone = _updater_sandbox(tmp_path)
    (clone / "local_note.txt").write_text("keep me", encoding="ascii")
    result = _run_updater(clone)
    assert result.returncode == 1
    assert "Uncommitted changes" in result.stdout
    assert "STUB_LAUNCHED" not in result.stdout
    assert (clone / "local_note.txt").read_text(encoding="ascii") == "keep me"


def test_all_windows_batch_files_are_ascii_crlf():
    # UTF-8 Japanese after "chcp 65001" makes cmd.exe execute fragments of echo lines as commands.
    for path in sorted(ROOT.glob("*.bat")):
        payload = path.read_bytes()
        payload.decode("ascii")
        assert b"\n" not in payload.replace(b"\r\n", b""), path.name


def test_launch_v2_disables_streamlit_first_run_email_prompt(monkeypatch):
    from scripts import launch_v2

    calls = []
    monkeypatch.setattr(launch_v2, "load_runtime_env", lambda root: True)
    monkeypatch.setattr(launch_v2, "choose_port", lambda preferred: 8507)
    monkeypatch.setattr(launch_v2.subprocess, "call", lambda command, cwd: calls.append(command) or 0)
    monkeypatch.setattr(launch_v2.sys, "argv", ["launch_v2.py"])
    assert launch_v2.main() == 0
    command = calls[0]
    assert command[command.index("--server.showEmailPrompt") + 1] == "false"
    assert command[command.index("--server.port") + 1] == "8507"
    assert "--server.headless" not in command
