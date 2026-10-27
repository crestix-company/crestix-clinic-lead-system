"""macOS LaunchAgent and local process state for the durable HP Auto Run worker."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time
from typing import Callable

from filelock import FileLock, Timeout


LAUNCH_AGENT_LABEL = "com.crestix.cliniclead.hp-auto-run-worker"
WORKER_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "hp_auto_run_worker.py"


@dataclass(frozen=True)
class WorkerPaths:
    state_dir: Path
    log_dir: Path
    lock_file: Path
    status_file: Path
    log_file: Path
    launch_agent_file: Path


def worker_paths(home: Path | None = None) -> WorkerPaths:
    home = Path(home or Path.home())
    state_dir = home / "Library" / "Application Support" / "ClinicLead" / "AutoHP"
    log_dir = home / "Library" / "Logs" / "ClinicLead"
    return WorkerPaths(
        state_dir=state_dir,
        log_dir=log_dir,
        lock_file=state_dir / "worker.lock",
        status_file=state_dir / "worker-status.json",
        log_file=log_dir / "hp-auto-run.log",
        launch_agent_file=home / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist",
    )


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        path.chmod(0o700)


def write_worker_status(paths: WorkerPaths, **values) -> None:
    _private_dir(paths.state_dir)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **values,
    }
    temporary = paths.status_file.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    if os.name != "nt":
        temporary.chmod(0o600)
    temporary.replace(paths.status_file)


def read_worker_status(paths: WorkerPaths) -> dict:
    try:
        payload = json.loads(paths.status_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def local_worker_running(paths: WorkerPaths) -> bool:
    """Probe the OS lock without relying on a potentially stale PID file."""
    if not paths.state_dir.exists():
        return False
    lock = FileLock(str(paths.lock_file))
    try:
        lock.acquire(timeout=0)
    except Timeout:
        return True
    else:
        lock.release()
        return False


def acquire_local_worker_lock(paths: WorkerPaths, *, timeout: float = 0):
    _private_dir(paths.state_dir)
    lock = FileLock(str(paths.lock_file))
    lock.acquire(timeout=timeout)
    return lock


def _run_launchctl(command: list[str], runner: Callable = subprocess.run):
    try:
        return runner(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise RuntimeError("macOSバックグラウンドWorkerを起動できません。launchctlの状態を確認してください。") from exc


def start_launch_agent(
    repo_root: Path,
    python_executable: str,
    *,
    paths: WorkerPaths | None = None,
    platform: str | None = None,
    uid: int | None = None,
    runner: Callable = subprocess.run,
    expected_run_id: str = "",
    wait_seconds: float = 15,
    sleeper: Callable = time.sleep,
) -> str:
    """Install or start the per-user LaunchAgent; never creates or changes a DB Job."""
    if (platform or sys.platform) != "darwin":
        raise RuntimeError("夜間Workerの常駐起動にはmacOSが必要です。")
    paths = paths or worker_paths()
    if local_worker_running(paths):
        worker_state = read_worker_status(paths)
        if expected_run_id and worker_state.get("run_id") == expected_run_id:
            return "already_running"
        deadline = time.monotonic() + max(0, float(wait_seconds))
        while local_worker_running(paths) and time.monotonic() < deadline:
            sleeper(0.25)
        if local_worker_running(paths):
            raise RuntimeError("別のHP Workerが稼働中です。現在のWorker状態を確認してください。")

    repo_root = Path(repo_root).resolve()
    _private_dir(paths.state_dir)
    _private_dir(paths.log_dir)
    _private_dir(paths.launch_agent_file.parent)
    plist = {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [str(python_executable), str(WORKER_SCRIPT)],
        "WorkingDirectory": str(repo_root),
        "RunAtLoad": True,
        # Expected exits (PAUSED/COMPLETED/IDLE) are not restarted. A crash is.
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
    }
    temporary = paths.launch_agent_file.with_suffix(".plist.tmp")
    temporary.write_bytes(plistlib.dumps(plist, fmt=plistlib.FMT_XML, sort_keys=True))
    temporary.chmod(0o600)
    temporary.replace(paths.launch_agent_file)

    service = f"gui/{uid if uid is not None else os.getuid()}/{LAUNCH_AGENT_LABEL}"
    print_result = _run_launchctl(["launchctl", "print", service], runner)
    if print_result.returncode == 0:
        started = _run_launchctl(["launchctl", "kickstart", service], runner)
    else:
        started = _run_launchctl(
            ["launchctl", "bootstrap", f"gui/{uid if uid is not None else os.getuid()}", str(paths.launch_agent_file)],
            runner,
        )
        if started.returncode != 0:
            # A concurrent UI session may have bootstrapped between print/bootstrap.
            refreshed = _run_launchctl(["launchctl", "print", service], runner)
            if refreshed.returncode == 0:
                started = _run_launchctl(["launchctl", "kickstart", service], runner)
    if started.returncode != 0:
        raise RuntimeError("macOSバックグラウンドWorkerを登録できません。詳細設定とシステムログを確認してください。")
    return "started"
