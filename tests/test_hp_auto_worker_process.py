import plistlib
from pathlib import Path

import pytest

from src.master.hp_auto_worker_process import (
    LAUNCH_AGENT_LABEL,
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
    commands = []

    class Result:
        def __init__(self, returncode):
            self.returncode = returncode
            self.stdout = ""
            self.stderr = ""

    def runner(command, **_kwargs):
        commands.append(command)
        return Result(0 if "bootstrap" in command else 1)

    assert start_launch_agent(
        Path("/private/repo"), "/private/python", paths=paths, platform="darwin",
        uid=501, runner=runner,
    ) == "started"
    with paths.launch_agent_file.open("rb") as stream:
        config = plistlib.load(stream)
    assert config["Label"] == LAUNCH_AGENT_LABEL
    assert config["KeepAlive"] == {"SuccessfulExit": False}
    assert config["ProgramArguments"][0] == "/private/python"
    assert all("DATABASE_URL" not in str(value) and "postgres://" not in str(value) for value in config.values())
    assert commands[0] == ["launchctl", "print", f"gui/501/{LAUNCH_AGENT_LABEL}"]
    assert commands[1][0:2] == ["launchctl", "bootstrap"]
    assert paths.launch_agent_file.stat().st_mode & 0o777 == 0o600


def test_launch_agent_refuses_non_macos_without_writing_files(tmp_path):
    paths = worker_paths(tmp_path)
    with pytest.raises(RuntimeError, match="macOS"):
        start_launch_agent(Path("/repo"), "/python", paths=paths, platform="linux")
    assert not paths.launch_agent_file.exists()
