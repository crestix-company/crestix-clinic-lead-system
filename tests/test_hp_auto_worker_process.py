import plistlib
from pathlib import Path
import sys

import pytest

from src.master.hp_auto_worker_process import (
    LAUNCH_AGENT_LABEL,
    LaunchAgentStartError,
    acquire_local_worker_lock,
    local_worker_running,
    read_worker_status,
    start_launch_agent,
    worker_paths,
    write_worker_status,
)


def test_local_worker_lock_and_private_status_are_recoverable(tmp_path):
    paths = worker_paths(tmp_path)
    assert not local_worker_running(paths)
    lock = acquire_local_worker_lock(paths)
    try:
        assert local_worker_running(paths)
    finally:
        lock.release()
    assert not local_worker_running(paths)

    write_worker_status(paths, status="RUNNING", run_id="run-safe", pid=12)
    assert read_worker_status(paths)["run_id"] == "run-safe"
    assert paths.status_file.stat().st_mode & 0o777 == 0o600
    assert paths.state_dir.stat().st_mode & 0o777 == 0o700


def test_launch_agent_is_per_user_and_contains_no_database_secret(tmp_path):
    paths = worker_paths(tmp_path)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    worker_script = repo_root / "worker.py"
    worker_script.write_text("pass\n", encoding="utf-8")
    commands = []
    held_locks = []

    class Result:
        def __init__(self, returncode):
            self.returncode = returncode
            self.stdout = ""
            self.stderr = ""

    def runner(command, **_kwargs):
        commands.append(command)
        if "bootstrap" in command:
            held_locks.append(acquire_local_worker_lock(paths))
        return Result(0 if "bootstrap" in command else 1)

    try:
        assert start_launch_agent(
            repo_root, sys.executable, paths=paths, platform="darwin", uid=501,
            runner=runner, worker_script=worker_script,
        ) == "started"
    finally:
        for lock in held_locks:
            lock.release()
    with paths.launch_agent_file.open("rb") as stream:
        config = plistlib.load(stream)
    assert config["Label"] == LAUNCH_AGENT_LABEL
    assert config["KeepAlive"] == {"SuccessfulExit": False}
    assert config["ProgramArguments"] == [sys.executable, str(worker_script)]
    assert all("DATABASE_URL" not in str(value) and "postgres://" not in str(value) for value in config.values())
    assert commands[0] == ["launchctl", "print", f"gui/501/{LAUNCH_AGENT_LABEL}"]
    assert commands[1][0:2] == ["launchctl", "bootstrap"]
    assert paths.launch_agent_file.stat().st_mode & 0o777 == 0o600


def test_launch_agent_refuses_non_macos_without_writing_files(tmp_path):
    paths = worker_paths(tmp_path)
    with pytest.raises(RuntimeError, match="macOS"):
        start_launch_agent(Path("/repo"), "/python", paths=paths, platform="linux")
    assert not paths.launch_agent_file.exists()


def test_launchctl_bootstrap_failure_exposes_safe_stage_and_return_code(tmp_path):
    paths = worker_paths(tmp_path)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    worker_script = repo_root / "worker.py"
    worker_script.write_text("pass\n", encoding="utf-8")

    class Result:
        stdout = ""
        def __init__(self, returncode, stderr=""):
            self.returncode = returncode
            self.stderr = stderr

    def runner(command, **_kwargs):
        if command[1] == "bootstrap":
            return Result(5, "Bootstrap failed: 5: Input/output error")
        return Result(1, "Could not find service")

    with pytest.raises(LaunchAgentStartError) as caught:
        start_launch_agent(
            repo_root, sys.executable, paths=paths, platform="darwin", uid=501,
            runner=runner, worker_script=worker_script, wait_seconds=0,
        )
    assert (caught.value.stage, caught.value.code, caught.value.returncode) == (
        "bootstrap", "input_output_error", 5,
    )
    assert "Input/output error" not in str(caught.value)


def test_successful_launchctl_submission_must_reach_worker_lock(tmp_path):
    paths = worker_paths(tmp_path)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    worker_script = repo_root / "worker.py"
    worker_script.write_text("pass\n", encoding="utf-8")

    class Result:
        returncode = 0
        stdout = stderr = ""

    with pytest.raises(LaunchAgentStartError, match="worker_lock_timeout"):
        start_launch_agent(
            repo_root, sys.executable, paths=paths, platform="darwin", uid=501,
            runner=lambda *_args, **_kwargs: Result(), worker_script=worker_script,
            wait_seconds=0,
        )
