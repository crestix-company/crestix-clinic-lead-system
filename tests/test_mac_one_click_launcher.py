import os
import shutil
import stat
import subprocess

import pytest

from src.utils.config import ROOT

UPDATE_SCRIPT = "scripts/update_and_launch_mac.sh"
LAUNCHER_CREATOR_SCRIPT = "scripts/create_mac_desktop_launcher.sh"
SETUP_SCRIPT = "scripts/setup_mac_one_click.sh"
MAC_SCRIPTS = (
    UPDATE_SCRIPT,
    LAUNCHER_CREATOR_SCRIPT,
    SETUP_SCRIPT,
    "scripts/launch_v2_mac.sh",
)
DESTRUCTIVE_GIT = (
    "git reset --hard",
    "git clean",
    "git checkout -- .",
    "git restore .",
    "git rebase",
    "git merge",
    "git stash",
    "push --force",
    "push -f",
)
BASH = shutil.which("bash")
mac_only = pytest.mark.skipif(os.name != "posix" or BASH is None, reason="requires a POSIX shell (bash)")


def _text(relative):
    return (ROOT / relative).read_text(encoding="utf-8")


def _run_bash(args, cwd=None, env=None):
    return subprocess.run(
        [BASH, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_mac_scripts_have_valid_bash_syntax():
    for relative in MAC_SCRIPTS:
        result = subprocess.run([BASH, "-n", str(ROOT / relative)], capture_output=True, text=True)
        assert result.returncode == 0, (relative, result.stderr)


def test_mac_scripts_are_executable_in_the_repo():
    for relative in MAC_SCRIPTS:
        mode = (ROOT / relative).stat().st_mode
        assert mode & stat.S_IXUSR, relative


def test_update_launcher_uses_set_euo_pipefail_and_resolves_repo_root_from_script_location():
    text = _text(UPDATE_SCRIPT)
    assert "set -euo pipefail" in text
    assert 'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"' in text
    assert 'REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"' in text


def test_update_launcher_checks_git_and_dotgit_before_mutating_anything():
    text = _text(UPDATE_SCRIPT)
    assert "command -v git" in text
    assert '[ ! -d "$REPO_ROOT/.git" ]' in text
    assert text.index("command -v git") < text.index("git switch main")
    assert text.index('[ ! -d "$REPO_ROOT/.git" ]') < text.index("git switch main")


def test_update_launcher_keeps_ff_only_and_no_destructive_git():
    for relative in MAC_SCRIPTS:
        text = _text(relative).lower()
        for command in DESTRUCTIVE_GIT:
            assert command not in text, (relative, command)
    update = _text(UPDATE_SCRIPT)
    assert "git status --porcelain" in update
    assert "git switch main" in update
    assert "git fetch origin main" in update
    assert "git pull --ff-only origin main" in update
    assert update.index("git status --porcelain") < update.index("git switch main")
    assert update.index("git switch main") < update.index("git fetch origin main")
    assert update.index("git fetch origin main") < update.index("git pull --ff-only origin main")


def test_update_launcher_hands_off_to_launch_v2_mac_at_the_end():
    update = _text(UPDATE_SCRIPT)
    assert 'exec "$SCRIPT_DIR/launch_v2_mac.sh"' in update
    assert update.index("git pull --ff-only origin main") < update.index('exec "$SCRIPT_DIR/launch_v2_mac.sh"')


def test_no_credentials_in_mac_scripts():
    for relative in MAC_SCRIPTS:
        text = _text(relative)
        assert "postgresql://" not in text
        assert "SUPABASE_RUNTIME_DB_URL" not in text
        assert "EMAIL_RESEARCH_DB_URL" not in text
        assert "SERPAPI_API_KEY" not in text


def test_desktop_launcher_creator_has_no_hardcoded_username():
    text = _text(LAUNCHER_CREATOR_SCRIPT)
    assert "/Users/" not in text
    assert "$HOME" in text


def test_desktop_launcher_creator_targets_update_and_launch_mac_without_arguments():
    text = _text(LAUNCHER_CREATOR_SCRIPT)
    assert 'LAUNCHER_NAME="Clinic Lead System.command"' in text
    assert "RUNTIME_DIR/scripts/update_and_launch_mac.sh" in text
    assert "chmod +x" in text


def test_setup_script_never_mutates_source_repo_git_state():
    text = _text(SETUP_SCRIPT)
    for forbidden in ("git -C \"$SOURCE_REPO_ROOT\" pull", "git -C \"$SOURCE_REPO_ROOT\" fetch",
                       "git -C \"$SOURCE_REPO_ROOT\" switch", "git -C \"$SOURCE_REPO_ROOT\" checkout",
                       "git -C \"$SOURCE_REPO_ROOT\" reset"):
        assert forbidden not in text
    assert 'git -C "$SOURCE_REPO_ROOT" remote get-url origin' in text


def test_setup_script_chmods_credential_files_600_and_checks_gitignore():
    text = _text(SETUP_SCRIPT)
    assert 'chmod 600 "$CRED_FILE"' in text
    assert "check-ignore" in text
    assert ".supabase-runtime.env.local" in text
    assert "email_research.env.local" in text


def test_setup_script_blocks_without_printing_or_prompting_for_credential_values():
    text = _text(SETUP_SCRIPT)
    assert "[BLOCKED]" in text
    assert "read -p" not in text
    assert "read " not in text


@mac_only
def test_update_launcher_stops_on_dirty_working_tree_without_changing_anything(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "scripts").mkdir()
    shutil.copy(ROOT / UPDATE_SCRIPT, repo / "scripts" / "update_and_launch_mac.sh")
    (repo / "scripts" / "launch_v2_mac.sh").write_text('#!/bin/bash\necho STUB_LAUNCHED\n', encoding="ascii")
    os.chmod(repo / "scripts" / "update_and_launch_mac.sh", 0o755)
    os.chmod(repo / "scripts" / "launch_v2_mac.sh", 0o755)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "seed")

    (repo / "dirty.txt").write_text("uncommitted", encoding="ascii")
    result = _run_bash([str(repo / "scripts" / "update_and_launch_mac.sh")], cwd=repo)

    assert result.returncode == 1
    assert "Uncommitted" in result.stderr or "未コミット" in result.stderr
    assert "STUB_LAUNCHED" not in result.stdout
    assert (repo / "dirty.txt").read_text(encoding="ascii") == "uncommitted"
    assert _git(repo, "status", "--porcelain").stdout.strip() != ""


@mac_only
def test_update_launcher_fast_forwards_and_hands_off_to_launch_v2_mac(tmp_path):
    origin, clone = _updater_sandbox(tmp_path)
    (origin / "NEW.txt").write_text("x", encoding="ascii")
    _git(origin, "add", ".")
    _git(origin, "commit", "-m", "new commit on origin")
    _git(origin, "push", "origin", "main")

    result = _run_bash([str(clone / "scripts" / "update_and_launch_mac.sh")], cwd=clone)

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "STUB_LAUNCHED" in result.stdout
    assert (clone / "NEW.txt").exists()


@mac_only
def test_setup_script_blocked_when_credential_missing_and_no_prompt_hangs(tmp_path):
    source_repo = tmp_path / "source_repo"
    _init_repo(source_repo)
    (source_repo / "scripts").mkdir()
    (source_repo / "config").mkdir()
    for relative in (SETUP_SCRIPT, LAUNCHER_CREATOR_SCRIPT, UPDATE_SCRIPT, "scripts/launch_v2_mac.sh"):
        dest = source_repo / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / relative, dest)
        os.chmod(dest, 0o755)
    (source_repo / "requirements-lock.txt").write_text("", encoding="ascii")
    _git(source_repo, "add", ".")
    _git(source_repo, "commit", "-m", "seed")

    origin_bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin_bare)], check=True, capture_output=True)
    _git(source_repo, "remote", "add", "origin", str(origin_bare))
    _git(source_repo, "push", "origin", "main")

    runtime_dir = tmp_path / "ClinicLeadRuntime"
    env = dict(os.environ)
    env["CLINIC_RUNTIME_DIR"] = str(runtime_dir)
    # No .supabase-runtime.env.local anywhere: setup must BLOCK, not hang on input.
    result = _run_bash([str(source_repo / SETUP_SCRIPT)], cwd=source_repo, env=env)

    assert result.returncode == 2
    assert "[BLOCKED]" in (result.stdout + result.stderr)
    assert "postgresql://" not in result.stdout
    assert "postgresql://" not in result.stderr


@mac_only
def test_desktop_launcher_creator_produces_executable_command_file(tmp_path):
    runtime_dir = tmp_path / "ClinicLeadRuntime"
    (runtime_dir / "scripts").mkdir(parents=True)
    (runtime_dir / "scripts" / "update_and_launch_mac.sh").write_text(
        '#!/bin/bash\necho STUB_LAUNCHED\n', encoding="ascii"
    )
    os.chmod(runtime_dir / "scripts" / "update_and_launch_mac.sh", 0o755)

    desktop_dir = tmp_path / "Desktop"
    env = dict(os.environ)
    env["CLINIC_DESKTOP_DIR"] = str(desktop_dir)

    creator = ROOT / LAUNCHER_CREATOR_SCRIPT
    result = _run_bash([str(creator), str(runtime_dir)], env=env)
    assert result.returncode == 0, (result.stdout, result.stderr)

    launcher_path = desktop_dir / "Clinic Lead System.command"
    assert launcher_path.is_file()
    mode = launcher_path.stat().st_mode
    assert mode & stat.S_IXUSR

    content = launcher_path.read_text(encoding="utf-8")
    assert str(runtime_dir) in content
    assert "postgresql://" not in content

    # Re-running is idempotent: it overwrites, not duplicates.
    result2 = _run_bash([str(creator), str(runtime_dir)], env=env)
    assert result2.returncode == 0
    assert list(desktop_dir.iterdir()) == [launcher_path]


@mac_only
def test_desktop_launcher_creator_fails_before_runtime_setup_exists(tmp_path):
    runtime_dir = tmp_path / "ClinicLeadRuntime"  # does not exist
    desktop_dir = tmp_path / "Desktop"
    env = dict(os.environ)
    env["CLINIC_DESKTOP_DIR"] = str(desktop_dir)

    result = _run_bash([str(ROOT / LAUNCHER_CREATOR_SCRIPT), str(runtime_dir)], env=env)

    assert result.returncode == 1
    assert not desktop_dir.exists() or not any(desktop_dir.iterdir())


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo(path):
    path.mkdir(parents=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")


def _updater_sandbox(tmp_path):
    """Bare origin + a clone containing the real updater and a stub launcher that only reports."""
    origin_bare = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin_bare)], check=True, capture_output=True)
    _init_repo(seed)
    (seed / "scripts").mkdir()
    shutil.copy(ROOT / UPDATE_SCRIPT, seed / "scripts" / "update_and_launch_mac.sh")
    os.chmod(seed / "scripts" / "update_and_launch_mac.sh", 0o755)
    (seed / "scripts" / "launch_v2_mac.sh").write_text('#!/bin/bash\necho STUB_LAUNCHED\n', encoding="ascii")
    os.chmod(seed / "scripts" / "launch_v2_mac.sh", 0o755)
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "remote", "add", "origin", str(origin_bare))
    _git(seed, "push", "origin", "main")

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(origin_bare), str(clone)], check=True, capture_output=True)
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "t")
    return seed, clone
