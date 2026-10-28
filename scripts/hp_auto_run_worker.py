#!/usr/bin/env python3
"""Detached per-user macOS worker for persisted HP Auto Run checkpoints."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
from threading import Event, Thread
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.master.hp_auto_worker_process import (  # noqa: E402
    acquire_local_worker_lock,
    worker_paths,
    write_worker_status,
)


class _SafeWorkerLogFilter(logging.Filter):
    """Keep only records whose message templates are designed to exclude clinic data."""
    ALLOWED = {"clinic.hp_auto_worker", "src.repository.hp_auto_run_repository"}

    def filter(self, record):
        return record.name in self.ALLOWED


def _configure_logging(log_path: Path):
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        log_path.parent.chmod(0o700)
        os.umask(0o077)
    handler = RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8", delay=False,
    )
    if os.name != "nt":
        log_path.chmod(0o600)
    handler.addFilter(_SafeWorkerLogFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.WARNING)
    root.addHandler(handler)
    return logging.getLogger("clinic.hp_auto_worker")


def _safe_error_fields(exc):
    diag = getattr(exc, "diag", None)
    return {
        "exception_type": type(exc).__name__,
        "sqlstate": str(getattr(exc, "sqlstate", "") or ""),
        "schema": str(getattr(diag, "schema_name", "") or ""),
        "table": str(getattr(diag, "table_name", "") or ""),
        "column": str(getattr(diag, "column_name", "") or ""),
    }


def _heartbeat(paths, stop, worker_id, run_id):
    while not stop.wait(10):
        write_worker_status(
            paths, status="RUNNING", pid=os.getpid(), worker_id=worker_id,
            run_id=run_id, heartbeat_at=time.time(),
        )


def _persist_setup_failure(root, run_id):
    """If the Writer remains reachable, mark only the just-started Run as ERROR."""
    try:
        from scripts.launch_v2_mac_normal import configure_normal_runtime
        configure_normal_runtime(root)
        from src.repository.write_backend import build_write_repositories
        repos = build_write_repositories(backend="supabase")
        return repos.auto_hp.mark_worker_start_failure(
            run_id, "Workerの接続準備に失敗しました。ログとネットワークを確認してください。"
        )
    except Exception:
        return False


def main():
    paths = worker_paths()
    logger = _configure_logging(paths.log_file)
    worker_id = uuid4().hex
    lock = None
    store = None
    stop_heartbeat = Event()
    heartbeat_thread = None
    stage = "local_lock"
    run_id = ""
    try:
        lock = acquire_local_worker_lock(paths, timeout=0)
    except Exception as exc:
        logger.info("Worker already active or lock unavailable exception_type=%s", type(exc).__name__)
        return 0

    try:
        write_worker_status(
            paths, status="STARTING", pid=os.getpid(), worker_id=worker_id,
            heartbeat_at=time.time(),
        )
        stage = "runtime_preflight"
        from scripts.launch_v2_mac_normal import configure_normal_runtime
        from src.master.hp_auto_run import NETWORK_BACKOFF_SECONDS, classify_controller_error
        preflight_attempt = 0
        while True:
            try:
                configure_normal_runtime(ROOT)
                break
            except Exception as exc:
                failure = classify_controller_error(exc)
                fields = _safe_error_fields(exc)
                preflight_attempt += 1
                if failure in {"temporary", "ambiguous"} and preflight_attempt <= len(NETWORK_BACKOFF_SECONDS):
                    delay = NETWORK_BACKOFF_SECONDS[preflight_attempt - 1]
                    logger.warning(
                        "Reader preflight retry=%s/%s exception_type=%s sqlstate=%s delay_seconds=%s",
                        preflight_attempt, len(NETWORK_BACKOFF_SECONDS),
                        fields["exception_type"], fields["sqlstate"], delay,
                    )
                    write_worker_status(
                        paths, status="WAITING_NETWORK", reason="runtime_preflight_retry",
                        retry_count=preflight_attempt, pid=os.getpid(), worker_id=worker_id,
                        heartbeat_at=time.time(),
                    )
                    time.sleep(delay)
                    continue
                write_worker_status(
                    paths, status="ERROR", reason="runtime_preflight_failed",
                    exception_type=fields["exception_type"], sqlstate=fields["sqlstate"],
                    stage=stage, retry_count=preflight_attempt, pid=os.getpid(),
                    worker_id=worker_id, heartbeat_at=time.time(),
                )
                logger.warning(
                    "Reader preflight stopped category=%s exception_type=%s sqlstate=%s",
                    failure, fields["exception_type"], fields["sqlstate"],
                )
                return 0

        stage = "reader_store"
        from src.repository.runtime_store import SupabaseRuntimeStore
        store = SupabaseRuntimeStore()
        state = store.hp_auto_run_state()
        run_id = str(state.get("run_id") or "")
        if state.get("status") not in {"RUNNING", "WAITING_NETWORK"}:
            write_worker_status(
                paths, status="STOPPED", reason=str(state.get("status", "IDLE")).lower(),
                pid=os.getpid(), worker_id=worker_id, run_id=run_id,
                heartbeat_at=time.time(),
            )
            return 0

        write_worker_status(
            paths, status="RUNNING", pid=os.getpid(), worker_id=worker_id,
            run_id=run_id, heartbeat_at=time.time(),
        )
        heartbeat_thread = Thread(
            target=_heartbeat, args=(paths, stop_heartbeat, worker_id, run_id),
            name="hp-auto-worker-heartbeat", daemon=True,
        )
        heartbeat_thread.start()

        stage = "controller_run"
        from src.master.hp_auto_run import AutoHpRunner
        result = AutoHpRunner().run_blocking(store)
        final_state = store.hp_auto_run_state()
        final_status = str(final_state.get("status", "UNKNOWN"))
        write_worker_status(
            paths,
            status="ERROR" if final_status == "ERROR" else "STOPPED",
            reason=result.get("reason", "finished"),
            failure_category=result.get("failure_category", ""),
            db_status=final_status,
            pid=os.getpid(), worker_id=worker_id, run_id=run_id,
            heartbeat_at=time.time(),
        )
        logger.info(
            "Worker exit reason=%s db_status=%s failure_category=%s",
            result.get("reason", "finished"), final_status,
            result.get("failure_category", ""),
        )
        return 0
    except Exception as exc:
        fields = _safe_error_fields(exc)
        logger.warning(
            "Worker stopped exception_type=%s sqlstate=%s stage=%s schema=%s table=%s column=%s",
            fields["exception_type"], fields["sqlstate"], stage,
            fields["schema"], fields["table"], fields["column"],
        )
        if stage in {"runtime_preflight", "reader_store"} and run_id:
            _persist_setup_failure(ROOT, run_id)
        write_worker_status(
            paths, status="ERROR", reason="worker_exception", exception_type=fields["exception_type"],
            sqlstate=fields["sqlstate"], stage=stage, pid=os.getpid(),
            worker_id=worker_id, run_id=run_id, heartbeat_at=time.time(),
        )
        # A bounded, handled setup/DB failure should not make launchd spin. A later
        # explicit Resume can kickstart the same persisted Run after the cause clears.
        return 0
    finally:
        stop_heartbeat.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=1)
        if store is not None:
            try:
                store._conn.close()
            except Exception:
                pass
        if lock is not None:
            lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
