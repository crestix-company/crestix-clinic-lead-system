"""Independent, resumable background Email Research controller."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Event, Lock, Thread
import socket
import time
import uuid

from src.master.email_research_config import EmailResearchConfig, load_email_research_config


@dataclass
class RunOutcome:
    processed: int = 0
    errors: int = 0
    metrics: dict | None = None

    def __post_init__(self):
        if self.metrics is None:
            self.metrics = {}

    def add_metrics(self, values):
        for key, value in (values or {}).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                self.metrics[key] = self.metrics.get(key, 0) + value


class EmailAutoRunner:
    def __init__(self, repository_factory, research_one, *, target_reader=None,
                 config: EmailResearchConfig | None = None):
        self.repository_factory = repository_factory
        self.research_one = research_one
        self.target_reader = target_reader
        self.config = config or load_email_research_config()
        self._stop = Event()
        self._thread = None
        self._lock = Lock()
        self._last_target_sync = 0.0
        self.controller_id = f"{socket.gethostname()}:{uuid.uuid4().hex}"
        self._controller_lost = Event()

    def running(self):
        return bool(self._thread and self._thread.is_alive())

    def start(self, run_id=None):
        with self._lock:
            if self.running():
                return False
            repo = None
            try:
                if run_id is None:
                    repo = self.repository_factory()
                    if self.target_reader is None or not hasattr(repo, "sync_targets"):
                        raise RuntimeError("Email Research Target Readerが設定されていません。")
                    sync_started = time.monotonic()
                    repo.sync_targets(self.target_reader())
                    self._initial_target_sync_ms = (time.monotonic() - sync_started) * 1000
                    self._last_target_sync = time.monotonic()
                    run_id = repo.create_auto_run(
                        workers=self.config.workers, timeout=self.config.read_timeout_seconds,
                        retries=self.config.retry_count, search_limit=self.config.search_query_limit,
                    )
            finally:
                self._close_repo(repo)
            self._stop.clear()
            self._controller_lost.clear()
            self._thread = Thread(target=self.run, args=(run_id,), daemon=True,
                                  name=f"email-auto-{run_id[:8]}")
            self._thread.start()
            return run_id

    def stop(self):
        self._stop.set()

    def recover(self, run_id):
        """Offer an existing durable run to this process; the DB lease picks Active/Standby."""
        return self.start(run_id)

    def pause(self, run_id):
        repo = self.repository_factory()
        try:
            repo.update_auto_run(run_id, status="PAUSED")
            self._stop.set()
        finally:
            self._close_repo(repo)

    def resume(self, run_id):
        repo = self.repository_factory()
        try:
            state = repo.auto_run(run_id) or {}
            if state.get("status") != "PAUSED":
                return False
            repo.update_auto_run(run_id, status="RUNNING", stop_requested=False)
        finally:
            self._close_repo(repo)
        return self.start(run_id)

    def kill(self, run_id):
        repo = self.repository_factory()
        try:
            repo.update_auto_run(run_id, status="STOPPED", stop_requested=True)
            self._stop.set()
        finally:
            self._close_repo(repo)

    @staticmethod
    def _close_repo(repo):
        if repo is not None and hasattr(repo, "close"):
            repo.close()

    def run(self, run_id):
        outcome = RunOutcome()
        control_repo = None
        heartbeat_stop = Event()
        heartbeat_thread = None
        try:
            control_repo = self.repository_factory()
            if hasattr(control_repo, "acquire_controller"):
                if control_repo.acquire_controller(run_id, self.controller_id) is None:
                    return outcome  # healthy controller on another PC: this process is standby

                def heartbeat_loop():
                    heartbeat_repo = None
                    try:
                        heartbeat_repo = self.repository_factory()
                        while not heartbeat_stop.wait(20):
                            if not heartbeat_repo.heartbeat_controller(run_id, self.controller_id):
                                self._controller_lost.set()
                                self._stop.set()
                                return
                    except Exception:
                        self._controller_lost.set()
                        self._stop.set()
                    finally:
                        self._close_repo(heartbeat_repo)

                heartbeat_thread = Thread(target=heartbeat_loop, daemon=True,
                                          name=f"email-heartbeat-{run_id[:8]}")
                heartbeat_thread.start()
            with ThreadPoolExecutor(max_workers=self.config.workers) as pool:
                results = list(pool.map(lambda index: self._worker(run_id, index),
                                        range(self.config.workers)))
            outcome.processed = sum(item.processed for item in results)
            outcome.errors = sum(item.errors for item in results)
            for item in results:
                outcome.add_metrics(item.metrics)
            if self._controller_lost.is_set():
                # Leave RUNNING + the existing checkpoint intact. Once the lease
                # expires another process reacquires the same run and stale claims.
                return outcome
            state = control_repo.auto_run(run_id) or {}
            final = state.get("status") if state.get("status") in {"PAUSED", "STOPPED"} else "COMPLETED"
            if final == "COMPLETED" and hasattr(control_repo, "finish_controller"):
                control_repo.finish_controller(run_id, self.controller_id, status=final)
            elif final in {"PAUSED", "STOPPED"}:
                pass
            else:
                control_repo.update_auto_run(run_id, status=final)
        except Exception as exc:
            if control_repo is not None:
                if hasattr(control_repo, "finish_controller"):
                    control_repo.finish_controller(
                        run_id, self.controller_id, status="ERROR",
                        error="Email Auto controllerで継続不能なエラーが発生しました。",
                    )
                else:
                    control_repo.update_auto_run(run_id, status="ERROR", error=str(exc))
            outcome.errors += 1
        finally:
            heartbeat_stop.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=1)
            self._close_repo(control_repo)
        return outcome

    def _worker(self, run_id, index):
        outcome = RunOutcome()
        worker_id = f"{socket.gethostname()}:{uuid.uuid4().hex}:{index}"
        repo = self.repository_factory()
        try:
            while not self._stop.is_set() and not self._controller_lost.is_set():
                state = repo.auto_run(run_id) or {}
                if state.get("stop_requested") or state.get("status") in {"PAUSED", "STOPPING", "ERROR"}:
                    break
                if (index == 0 and self.target_reader is not None
                        and time.monotonic() - self._last_target_sync >= self.config.target_sync_interval_seconds):
                    sync_started = time.monotonic()
                    repo.sync_targets(self.target_reader())
                    sync_ms = (time.monotonic() - sync_started) * 1000
                    if hasattr(repo, "record_target_sync"):
                        repo.record_target_sync(run_id, elapsed_ms=sync_ms)
                    self._last_target_sync = time.monotonic()
                claim_started = time.monotonic()
                claim = repo.claim_next(worker_id=worker_id, run_id=run_id,
                                        lease_seconds=self.config.claim_lease_seconds)
                claim_ms = (time.monotonic() - claim_started) * 1000
                if claim is None:
                    # A fixed Canary cohort stops when every member is terminal.
                    if hasattr(repo, "cohort_pending_count"):
                        pending = repo.cohort_pending_count()
                        if pending is not None and pending <= 0:
                            repo.update_auto_run(run_id, status="COMPLETED", stop_requested=True)
                            self._stop.set()
                            break
                    # Normal mode keeps refreshing HP-verified eligible targets.
                    if self.target_reader is None or not hasattr(repo, "sync_targets"):
                        break
                    state = repo.auto_run(run_id) or {}
                    if state.get("stop_requested") or state.get("status") != "RUNNING":
                        break
                    if self._stop.wait(self.config.idle_poll_seconds):
                        break
                    continue
                try:
                    clinic_started_at = datetime.now(timezone.utc).isoformat()
                    processing_started = time.monotonic()
                    result = self._research_with_claim_heartbeat(repo, claim)
                    if result is None:
                        outcome.errors += 1
                        continue
                    with self._lock:
                        initial_sync_ms = getattr(self, "_initial_target_sync_ms", 0.0)
                        self._initial_target_sync_ms = 0.0
                    metrics = dict(getattr(result, "metrics", {}) or {})
                    metrics.update({
                        "claim_ms": claim_ms, "db_read_ms": claim_ms,
                        "target_sync_ms": initial_sync_ms,
                        "clinic_started_at": clinic_started_at,
                    })
                    metrics["elapsed_ms"] = (time.monotonic() - processing_started) * 1000
                    write_started = time.monotonic()
                    repo.complete_claim(
                        clinic_id=claim["clinic_id"], claim_token=claim["claim_token"],
                        candidates=result.candidates, research_status=result.research_status,
                        metrics=metrics, research_run_id=run_id,
                        rejection_evidence=getattr(result, "rejection_evidence", []),
                        primary_minimum_confidence=self.config.primary_minimum_confidence,
                    )
                    metrics["db_write_ms"] = (time.monotonic() - write_started) * 1000
                    metrics["clinic_finished_at"] = datetime.now(timezone.utc).isoformat()
                    outcome.processed += 1
                    outcome.add_metrics(metrics)
                    if hasattr(repo, "record_auto_run_result"):
                        repo.record_auto_run_result(run_id, metrics=metrics,
                                                    research_status=result.research_status)
                except Exception as exc:
                    repo.fail_claim(clinic_id=claim["clinic_id"], claim_token=claim["claim_token"],
                                    error=str(exc))
                    outcome.errors += 1
                    if hasattr(repo, "record_auto_run_result"):
                        repo.record_auto_run_result(run_id, research_status="ERROR")
        finally:
            self._close_repo(repo)
        return outcome

    def _research_with_claim_heartbeat(self, worker_repo, claim):
        """Keep a long external crawl's claim alive using a dedicated DB connection."""
        if not hasattr(worker_repo, "renew_claim"):
            return self.research_one(claim, self.config)
        stopped = Event()
        lost = Event()

        def renew_loop():
            renew_repo = None
            try:
                renew_repo = self.repository_factory()
                interval = max(10, min(60, int(self.config.claim_lease_seconds) // 3))
                while not stopped.wait(interval):
                    if not renew_repo.renew_claim(
                        clinic_id=claim["clinic_id"], claim_token=claim["claim_token"],
                        lease_seconds=self.config.claim_lease_seconds,
                    ):
                        lost.set()
                        return
            except Exception:
                lost.set()
            finally:
                self._close_repo(renew_repo)

        thread = Thread(target=renew_loop, daemon=True,
                        name=f"email-claim-{claim['clinic_id']}")
        thread.start()
        try:
            result = self.research_one(claim, self.config)
            return None if lost.is_set() else result
        finally:
            stopped.set()
            thread.join(timeout=1)


class MeteredTransport:
    """Separate public-web fetch accounting from paid Search API accounting."""
    def __init__(self, transport):
        self.transport = transport
        self.metrics = {"web_fetch_attempts": 0, "web_fetch_errors": 0, "web_fetch_timeout": 0}

    def get(self, *args, **kwargs):
        self.metrics["web_fetch_attempts"] += 1
        try:
            return self.transport.get(*args, **kwargs)
        except Exception as exc:
            self.metrics["web_fetch_errors"] += 1
            # SafeFetcher/PinnedTransport expose timeout failures as a bounded,
            # sanitized WebError string; do not retain request content or URLs.
            if any(token in str(exc).casefold() for token in ("timeout", "timed out", "時間上限", "タイムアウト")):
                self.metrics["web_fetch_timeout"] += 1
            raise


def build_default_research_one(*, trusted_candidate_provider=None):
    """Construct the proven engine with runtime config; no DB writes occur here."""
    def research_one(claim, config):
        from src.enrichment.email_research.formal_pipeline import research_clinic
        from src.enrichment.safe_web import PinnedTransport, SafeFetcher
        from src.enrichment.search_provider import SerpApiSearchProvider
        from src.normalizer.address import extract_municipality

        provider = SerpApiSearchProvider(
            connect_timeout=config.connect_timeout_seconds,
            read_timeout=config.read_timeout_seconds, max_retries=config.retry_count,
        )
        transport = MeteredTransport(PinnedTransport())
        fetcher = SafeFetcher(transport=transport, timeout=config.read_timeout_seconds)
        clinic = dict(claim)
        clinic["city"] = clinic.get("city") or extract_municipality(clinic.get("address") or "")
        trusted = trusted_candidate_provider(clinic) if trusted_candidate_provider else ()
        return research_clinic(clinic, fetcher=fetcher, search_provider=provider,
                               trusted_candidates=trusted)
    return research_one
