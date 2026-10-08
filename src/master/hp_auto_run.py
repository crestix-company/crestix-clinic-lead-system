"""Durable Auto HP batch controller.

The database is the source of truth.  This thread is only an executor: every
process/PC must acquire the persisted controller lease before it may run or
chain a child job.
"""
from __future__ import annotations

import socket
from threading import Event, Lock, Thread
import time
import uuid

from src.enrichment.search_provider import TavilySearchProvider
from src.master.jobs import job_status, run_job
from src.repository.write_backend import write_repositories_for


NETWORK_BACKOFF_SECONDS = (30, 60, 60)


def classify_controller_error(exc):
    """Return temporary/ambiguous/fatal/non_network without exposing details."""
    text = str(exc).lower()
    sqlstate = str(getattr(exc, "sqlstate", "") or "")
    if sqlstate.startswith("28") or any(word in text for word in ("authentication failed", "password authentication", "permission denied")):
        return "fatal"
    if isinstance(exc, socket.gaierror) or any(word in text for word in ("name or service not known", "nodename nor servname", "temporary failure in name resolution")):
        return "ambiguous"
    if isinstance(exc, (ConnectionError, TimeoutError)) or any(word in text for word in (
        "connection timed out", "connection refused", "server closed the connection",
        "network is unreachable", "connection reset", "could not connect",
    )):
        return "temporary"
    return "non_network"


class AutoHpRunner:
    def __init__(self, *, sleeper=time.sleep, backoffs=NETWORK_BACKOFF_SECONDS):
        self.thread = None
        self.lock = Lock()
        self.sleeper = sleeper
        self.backoffs = tuple(backoffs)
        self.controller_id = uuid.uuid4().hex

    def running(self):
        return bool(self.thread and self.thread.is_alive())

    def start(self, store):
        with self.lock:
            if self.running():
                return False
            self.thread = Thread(target=self._run, args=(store,), daemon=True)
            self.thread.start()
            return True

    def _repo(self, store):
        repo = write_repositories_for(store).auto_hp
        if repo is None:
            raise RuntimeError("Auto HP Research requires the Supabase runtime.")
        return repo

    def _run_child(self, store, jid):
        """Keep the controller lease alive even while one HTTP crawl is lengthy."""
        stopped = Event()
        heartbeat_error = []

        def heartbeat_loop():
            while not stopped.wait(20):
                try:
                    if not self._repo(store).heartbeat(self.controller_id):
                        heartbeat_error.append(RuntimeError("Auto Run controller leaseを失いました。"))
                        return
                except Exception as exc:
                    heartbeat_error.append(exc)
                    return

        heartbeat_thread = Thread(target=heartbeat_loop, daemon=True)
        heartbeat_thread.start()
        last_heartbeat = [0.0]

        def progress():
            if heartbeat_error:
                raise heartbeat_error[0]
            tick = time.monotonic()
            if tick - last_heartbeat[0] < 20:
                return True
            last_heartbeat[0] = tick
            return self._repo(store).heartbeat(self.controller_id)

        try:
            return run_job(
                store, jid, TavilySearchProvider(""), progress_callback=progress
            )
        finally:
            stopped.set()
            heartbeat_thread.join(timeout=1)

    def _run(self, store):
        retry_count = 0
        while True:
            try:
                repo = self._repo(store)
                state = repo.acquire(self.controller_id)
                if state is None:
                    return
                if retry_count and state.get("status") == "RUNNING":
                    # Supabase may have been unreachable when the outage began. Persist the
                    # recovery state as soon as the database is reachable again.
                    repo.set_controller_status(
                        self.controller_id, "WAITING_NETWORK", "通信回復を確認しました。", retry_count
                    )
                    state["status"] = "WAITING_NETWORK"
                if state.get("status") == "WAITING_NETWORK":
                    repo.set_controller_status(self.controller_id, "RUNNING", retry_count=0)
                    state["status"] = "RUNNING"
                jid = state.get("current_job_id")
                if not jid:
                    repo.set_controller_status(self.controller_id, "ERROR", "現在Jobを確認できません。")
                    return
                current = job_status(store, jid)
                if current["status"] == "COMPLETED":
                    next_state = repo.advance(self.controller_id)
                    if next_state.get("status") == "COMPLETED":
                        return
                    retry_count = 0
                    continue
                if current["status"] == "RESET":
                    repo.set_controller_status(self.controller_id, "ERROR", "現在Jobがリセットされています。")
                    return

                self._run_child(store, jid)
                current = job_status(store, jid)
                if current["status"] == "COMPLETED":
                    next_state = self._repo(store).advance(self.controller_id)
                    if next_state.get("status") == "COMPLETED":
                        return
                    retry_count = 0
                    continue
                latest = self._repo(store).get_state()
                if latest.get("status") in {"PAUSED", "CANCELLED", "COMPLETED", "ERROR"}:
                    return
                if current["status"] == "PAUSED" and latest.get("status") == "RUNNING":
                    # Resume may be requested while the old worker is still reaching its
                    # safe item boundary. Re-enter through the persisted lease contract.
                    continue
                if current["status"] == "RUNNING":
                    # Another fenced worker can still own an unexpired item lease.
                    self.sleeper(5)
                    continue
                self._repo(store).set_controller_status(
                    self.controller_id, "ERROR", "現在Batchを継続できない状態です。"
                )
                return
            except Exception as exc:
                kind = classify_controller_error(exc)
                if kind == "non_network" or kind == "fatal":
                    try:
                        self._repo(store).set_controller_status(
                            self.controller_id, "ERROR", "Auto Run controllerで継続不能なエラーが発生しました。"
                        )
                    except Exception:
                        pass
                    return
                retry_count += 1
                if kind == "ambiguous" and retry_count > len(self.backoffs):
                    try:
                        self._repo(store).set_controller_status(
                            self.controller_id, "ERROR", "DNS接続を確認できません。設定と通信環境を確認してください。"
                        )
                    except Exception:
                        pass
                    return
                try:
                    self._repo(store).set_controller_status(
                        self.controller_id, "WAITING_NETWORK", "通信回復を待っています。", retry_count
                    )
                except Exception:
                    # The state remains RUNNING while Supabase itself is unreachable.
                    # The next successful iteration persists the recovery transition.
                    pass
                delay = self.backoffs[min(retry_count - 1, len(self.backoffs) - 1)] if self.backoffs else 60
                self.sleeper(delay)
