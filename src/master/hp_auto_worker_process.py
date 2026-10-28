"""macOS LaunchAgent and local process state for the durable HP Auto Run worker."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import errno
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


class LaunchAgentStartError(RuntimeError):
    """Safe, structured launch failure details (never includes credentials or raw output)."""

    def __init__(self, stage: str, code: str, returncode: int | None = None):
        self.stage = stage
        self.code = code
        self.returncode = returncode
        super().__init__(f"LaunchAgent {stage} failed ({code})")


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
        code = "permission_denied" if exc.errno in {errno.EACCES, errno.EPERM} else f"os_error_{exc.errno or 0}"
        raise LaunchAgentStartError("launchctl_exec", code) from None


def _launchctl_error_code(result) -> str:
    diagnostic = f"{getattr(result, 'stdout', '')} {getattr(result, 'stderr', '')}".lower()
    if "input/output error" in diagnostic:
        return "input_output_error"
    if "bad ownership" in diagnostic or "permissions" in diagnostic:
        return "bad_ownership_or_permissions"
    if "not permitted" in diagnostic or "operation not permitted" in diagnostic:
        return "operation_not_permitted"
    if "already loaded" in diagnostic or "service already" in diagnostic:
        return "already_loaded"
    return f"exit_{int(getattr(result, 'returncode', 1) or 1)}"


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
    label: str = LAUNCH_AGENT_LABEL,
    worker_script: Path | None = None,
) -> str:
    """Install or start the per-user LaunchAgent; never creates or changes a DB Job."""
    if (platform or sys.platform) != "darwin":
        raise RuntimeError("夜間Workerの常駐起動にはmacOSが必要です。")
    paths = paths or worker_paths()
    worker_script = Path(worker_script or WORKER_SCRIPT).resolve()
    # Preserve the virtualenv entrypoint path: resolving a venv's interpreter
    # symlink can silently select the system Python and lose its site-packages.
    python_executable = os.path.abspath(os.path.expanduser(str(python_executable)))
    repo_root = Path(repo_root).resolve()
    if not repo_root.is_dir():
        raise LaunchAgentStartError("validate_paths", "working_directory_missing")
    if not worker_script.is_file():
        raise LaunchAgentStartError("validate_paths", "worker_script_missing")
    if not Path(python_executable).is_file() or not os.access(python_executable, os.X_OK):
        raise LaunchAgentStartError("validate_paths", "python_not_executable")
    if local_worker_running(paths):
        worker_state = read_worker_status(paths)
        if expected_run_id and worker_state.get("run_id") == expected_run_id:
            return "already_running"
        deadline = time.monotonic() + max(0, float(wait_seconds))
        while local_worker_running(paths) and time.monotonic() < deadline:
            sleeper(0.25)
        if local_worker_running(paths):
            raise RuntimeError("別のHP Workerが稼働中です。現在のWorker状態を確認してください。")

    try:
        _private_dir(paths.state_dir)
        _private_dir(paths.log_dir)
        _private_dir(paths.launch_agent_file.parent)
    except OSError as exc:
        code = "permission_denied" if exc.errno in {errno.EACCES, errno.EPERM} else f"os_error_{exc.errno or 0}"
        raise LaunchAgentStartError("prepare_directories", code) from None
    plist = {
        "Label": label,
        "ProgramArguments": [python_executable, str(worker_script)],
        "WorkingDirectory": str(repo_root),
        "RunAtLoad": True,
        # Expected exits (PAUSED/COMPLETED/IDLE) are not restarted. A crash is.
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
    }
    try:
        temporary = paths.launch_agent_file.with_suffix(".plist.tmp")
        temporary.write_bytes(plistlib.dumps(plist, fmt=plistlib.FMT_XML, sort_keys=True))
        temporary.chmod(0o600)
        temporary.replace(paths.launch_agent_file)
    except OSError as exc:
        code = "permission_denied" if exc.errno in {errno.EACCES, errno.EPERM} else f"os_error_{exc.errno or 0}"
        raise LaunchAgentStartError("write_plist", code) from None

    previous_status = read_worker_status(paths)
    previous_updated_at = str(previous_status.get("updated_at") or "")
    try:
        previous_status_mtime = paths.status_file.stat().st_mtime_ns
    except OSError:
        previous_status_mtime = 0
    service = f"gui/{uid if uid is not None else os.getuid()}/{label}"
    action = "kickstart"
    print_result = _run_launchctl(["launchctl", "print", service], runner)
    if print_result.returncode == 0:
        started = _run_launchctl(["launchctl", "kickstart", service], runner)
    else:
        action = "bootstrap"
        started = _run_launchctl(
            ["launchctl", "bootstrap", f"gui/{uid if uid is not None else os.getuid()}", str(paths.launch_agent_file)],
            runner,
        )
        if started.returncode != 0:
            # A concurrent UI session may have bootstrapped between print/bootstrap.
            refreshed = _run_launchctl(["launchctl", "print", service], runner)
            if refreshed.returncode == 0:
                action = "kickstart"
                started = _run_launchctl(["launchctl", "kickstart", service], runner)
    if started.returncode != 0:
        raise LaunchAgentStartError(action, _launchctl_error_code(started), started.returncode)

    # launchctl returning zero only confirms job submission. Wait until the worker
    # actually takes its private lock, or reports that startup failed and exited.
    deadline = time.monotonic() + max(0, float(wait_seconds))
    while True:
        if local_worker_running(paths):
            return "started"
        worker_state = read_worker_status(paths)
        try:
            status_mtime = paths.status_file.stat().st_mtime_ns
        except OSError:
            status_mtime = 0
        is_fresh_status = status_mtime > previous_status_mtime or (
            bool(worker_state.get("updated_at")) and str(worker_state.get("updated_at")) != previous_updated_at
        )
        status_matches_run = not expected_run_id or worker_state.get("run_id") in {"", expected_run_id}
        if is_fresh_status and status_matches_run and worker_state.get("status") == "ERROR":
            raise LaunchAgentStartError(
                "worker_start", str(worker_state.get("reason") or "worker_exited_before_lock")[:80]
            )
        if time.monotonic() >= deadline:
            raise LaunchAgentStartError("worker_start", "worker_lock_timeout")
        sleeper(0.1)
