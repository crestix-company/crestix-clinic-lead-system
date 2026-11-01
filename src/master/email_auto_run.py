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
    controller_heartbeat_interval_seconds = 20.0
    lease_retry_base_seconds = 0.5

    def __init__(self, repository_factory, research_one, *, target_reader=None,
                 config: EmailResearchConfig | None = None, max_claims_per_execution=None):
        self.repository_factory = repository_factory
        self.research_one = research_one
        self.target_reader = target_reader
        self.config = config or load_email_research_config()
        self.max_claims_per_execution = (
            int(max_claims_per_execution) if max_claims_per_execution is not None else None
        )
        if self.max_claims_per_execution is not None and self.max_claims_per_execution < 1:
            raise ValueError("max_claims_per_execution must be positive")
        self._execution_claim_lock = Lock()
        self._execution_claim_count = 0
        self._execution_limit_reached = Event()
        self._stop = Event()
        self._thread = None
        self._lock = Lock()
        self._last_target_sync = 0.0
        self.controller_id = f"{socket.gethostname()}:{uuid.uuid4().hex}"
        self._controller_lost = Event()
        # Safe, non-secret diagnostic state consumed by the Streamlit UI.  This is
        # deliberately a stage code rather than an exception/message: DB exception
        # text can contain endpoint or connection details.
        self.last_start_stage = "idle"

    @staticmethod
    def _is_connection_error(exc):
        """Only classify errors for which abandoning the old connection is safe.

        Writes are never blindly replayed here.  A completion with an unknown commit
        outcome is left behind its claim token and is reconciled by the durable DB
        state/lease on the next claim attempt.
        """
        try:
            import psycopg
        except ImportError:
            return False
        return isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError))

    def _replace_repo_after_connection_error(self, repo, failures):
        self._close_repo(repo)
        if self._stop.wait(min(5.0, 0.25 * (2 ** min(failures, 4)))):
            return None
        return self.repository_factory()

    def running(self):
        return bool(self._thread and self._thread.is_alive())

    def start(self, run_id=None):
        from src.safety.preview_mode import assert_not_comdesk_preview_only
        assert_not_comdesk_preview_only("Email自動実行")
        with self._lock:
            self.last_start_stage = "running_check"
            if self.running():
                self.last_start_stage = "already_running"
                return False
            repo = None
            try:
                if run_id is None:
                    self.last_start_stage = "email_repository"
                    repo = self.repository_factory()
                    if self.target_reader is None or not hasattr(repo, "sync_targets"):
                        raise RuntimeError("Email Research Target Readerが設定されていません。")
                    self.last_start_stage = "target_reader"
                    targets = self.target_reader()
                    sync_started = time.monotonic()
                    self.last_start_stage = "sync_targets"
                    repo.sync_targets(targets)
                    self._initial_target_sync_ms = (time.monotonic() - sync_started) * 1000
                    self._last_target_sync = time.monotonic()
                    self.last_start_stage = "create_auto_run"
                    run_id = repo.create_auto_run(
                        workers=self.config.workers, timeout=self.config.read_timeout_seconds,
                        retries=self.config.retry_count, search_limit=self.config.search_query_limit,
                    )
                else:
                    self.last_start_stage = "recover_existing_run"
            finally:
                self._close_repo(repo)
            self._stop.clear()
            self._controller_lost.clear()
            self._thread = Thread(target=self.run, args=(run_id,), daemon=True,
                                  name=f"email-auto-{run_id[:8]}")
            self._thread.start()
            self.last_start_stage = "started"
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
        with self._execution_claim_lock:
            self._execution_claim_count = 0
        self._execution_limit_reached.clear()
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
                    while not heartbeat_stop.wait(self.controller_heartbeat_interval_seconds):
                        renewed = False
                        for attempt in range(3):
                            heartbeat_repo = None
                            try:
                                # A fresh connection per heartbeat prevents a stale
                                # long-lived socket from killing an otherwise healthy run.
                                heartbeat_repo = self.repository_factory()
                                renewed = bool(heartbeat_repo.heartbeat_controller(
                                    run_id, self.controller_id))
                                break
                            except Exception as exc:
                                if not self._is_connection_error(exc):
                                    break
                                if heartbeat_stop.wait(self.lease_retry_base_seconds * (attempt + 1)):
                                    return
                            finally:
                                self._close_repo(heartbeat_repo)
                        if not renewed:
                            self._controller_lost.set()
                            self._stop.set()
                            return

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
        repo = None
        connection_failures = 0
        try:
            while not self._stop.is_set() and not self._controller_lost.is_set():
                try:
                    if repo is None:
                        repo = self.repository_factory()
                    state = repo.auto_run(run_id) or {}
                except Exception as exc:
                    if not self._is_connection_error(exc):
                        outcome.errors += 1
                        break
                    connection_failures += 1
                    try:
                        repo = self._replace_repo_after_connection_error(repo, connection_failures)
                    except Exception as reconnect_exc:
                        repo = None
                        if not self._is_connection_error(reconnect_exc):
                            outcome.errors += 1
                            break
                    continue
                connection_failures = 0
                if state.get("stop_requested") or state.get("status") in {"PAUSED", "STOPPING", "ERROR"}:
                    break
                try:
                    if (index == 0 and self.target_reader is not None
                            and time.monotonic() - self._last_target_sync >= self.config.target_sync_interval_seconds):
                        sync_started = time.monotonic()
                        repo.sync_targets(self.target_reader())
                        sync_ms = (time.monotonic() - sync_started) * 1000
                        if hasattr(repo, "record_target_sync"):
                            repo.record_target_sync(run_id, elapsed_ms=sync_ms)
                        self._last_target_sync = time.monotonic()
                    claim_started = time.monotonic()
                    if self.max_claims_per_execution is None:
                        claim = repo.claim_next(worker_id=worker_id, run_id=run_id,
                                                lease_seconds=self.config.claim_lease_seconds)
                    else:
                        # Serialize only the short claim operation so concurrent workers
                        # cannot claim beyond the per-Execution safety ceiling.
                        with self._execution_claim_lock:
                            if self._execution_claim_count >= self.max_claims_per_execution:
                                self._execution_limit_reached.set()
                                claim = None
                            else:
                                claim = repo.claim_next(worker_id=worker_id, run_id=run_id,
                                                        lease_seconds=self.config.claim_lease_seconds)
                                if claim is not None:
                                    self._execution_claim_count += 1
                except Exception as exc:
                    if self._is_connection_error(exc):
                        connection_failures += 1
                        try:
                            repo = self._replace_repo_after_connection_error(repo, connection_failures)
                        except Exception as reconnect_exc:
                            repo = None
                            if not self._is_connection_error(reconnect_exc):
                                outcome.errors += 1
                                break
                        continue
                    outcome.errors += 1
                    # A worker-local repository/query failure must not terminate the
                    # other workers or incorrectly mark the durable run completed.
                    if self._stop.wait(self.config.idle_poll_seconds):
                        break
                    continue
                claim_ms = (time.monotonic() - claim_started) * 1000
                if claim is None:
                    if self._execution_limit_reached.is_set():
                        repo.update_auto_run(run_id, status="PAUSED", stop_requested=False)
                        self._stop.set()
                        break
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
                    metrics["clinic_finished_at"] = datetime.now(timezone.utc).isoformat()
                    write_started = time.monotonic()
                    completion = repo.complete_claim(
                        clinic_id=claim["clinic_id"], claim_token=claim["claim_token"],
                        candidates=result.candidates, research_status=result.research_status,
                        metrics=metrics, research_run_id=run_id,
                        rejection_evidence=getattr(result, "rejection_evidence", []),
                        primary_minimum_confidence=self.config.primary_minimum_confidence,
                    )
                    metrics["db_write_ms"] = float(
                        (completion or {}).get("db_write_ms",
                                               (time.monotonic() - write_started) * 1000))
                    outcome.processed += 1
                    outcome.add_metrics(metrics)
                    if (hasattr(repo, "record_auto_run_result")
                            and not getattr(repo, "complete_claim_records_run_metrics", False)):
                        repo.record_auto_run_result(run_id, metrics=metrics,
                                                    research_status=result.research_status)
                except Exception as exc:
                    outcome.errors += 1
                    if self._is_connection_error(exc):
                        # The commit outcome is unknown. Never replay the write or turn
                        # it into ERROR; a committed result remains terminal, otherwise
                        # the fenced claim becomes reclaimable after lease expiry.
                        connection_failures += 1
                        try:
                            repo = self._replace_repo_after_connection_error(repo, connection_failures)
                        except Exception:
                            repo = None
                        continue
                    try:
                        repo.fail_claim(
                            clinic_id=claim["clinic_id"], claim_token=claim["claim_token"],
                            error=str(exc), metrics={"clinic_started_at": clinic_started_at},
                            research_run_id=run_id,
                        )
                        if (hasattr(repo, "record_auto_run_result")
                                and not getattr(repo, "complete_claim_records_run_metrics", False)):
                            repo.record_auto_run_result(run_id, research_status="ERROR")
                    except TypeError:
                        # Compatibility with small repository fakes and older adapters.
                        try:
                            repo.fail_claim(
                                clinic_id=claim["clinic_id"], claim_token=claim["claim_token"],
                                error=str(exc))
                        except Exception as failure_exc:
                            if self._is_connection_error(failure_exc):
                                connection_failures += 1
                                try:
                                    repo = self._replace_repo_after_connection_error(
                                        repo, connection_failures)
                                except Exception:
                                    repo = None
                    except Exception as failure_exc:
                        if self._is_connection_error(failure_exc):
                            connection_failures += 1
                            try:
                                repo = self._replace_repo_after_connection_error(
                                    repo, connection_failures)
                            except Exception:
                                repo = None
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
            interval = max(10, min(60, int(self.config.claim_lease_seconds) // 3))
            while not stopped.wait(interval):
                renewed = False
                for attempt in range(3):
                    renew_repo = None
                    try:
                        renew_repo = self.repository_factory()
                        renewed = bool(renew_repo.renew_claim(
                            clinic_id=claim["clinic_id"], claim_token=claim["claim_token"],
                            lease_seconds=self.config.claim_lease_seconds,
                        ))
                        break
                    except Exception as exc:
                        if not self._is_connection_error(exc):
                            break
                        if stopped.wait(self.lease_retry_base_seconds * (attempt + 1)):
                            return
                    finally:
                        self._close_repo(renew_repo)
                if not renewed:
                    lost.set()
                    return

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
    def __init__(self, transport, *, max_retries=1, backoff_seconds=0.5, sleeper=time.sleep):
        self.transport = transport
        self.max_retries = max(0, int(max_retries))
        self.backoff_seconds = max(0.0, float(backoff_seconds))
        self.sleep = sleeper
        self.metrics = {
            "web_fetch_attempts": 0, "web_fetch_errors": 0,
            "web_fetch_timeout": 0, "web_fetch_dns_error": 0,
            "web_fetch_ssl_error": 0, "web_fetch_http_5xx": 0,
            "web_fetch_retry_count": 0,
        }

    def get(self, *args, **kwargs):
        retries = 0
        while True:
            self.metrics["web_fetch_attempts"] += 1
            try:
                response = self.transport.get(*args, **kwargs)
            except Exception as exc:
                self.metrics["web_fetch_errors"] += 1
                message = str(exc).casefold()
                is_ssl = "ssl証明書" in message or "certificate" in message
                is_dns = "dns" in message or "名前解決" in message
                is_timeout = any(token in message for token in (
                    "timeout", "timed out", "時間上限", "タイムアウト"))
                if is_ssl:
                    self.metrics["web_fetch_ssl_error"] += 1
                if is_dns:
                    self.metrics["web_fetch_dns_error"] += 1
                if is_timeout:
                    self.metrics["web_fetch_timeout"] += 1
                # Certificate validation is permanent/security-sensitive. DNS,
                # timeout and sanitized connection failures get a bounded retry.
                retryable = not is_ssl and (
                    is_dns or is_timeout or "接続・ssl・タイムアウト" in message)
                if retryable and retries < self.max_retries:
                    self.metrics["web_fetch_retry_count"] += 1
                    if self.backoff_seconds:
                        self.sleep(self.backoff_seconds * (2 ** retries))
                    retries += 1
                    continue
                raise
            if 500 <= int(getattr(response, "status", 0) or 0) < 600:
                self.metrics["web_fetch_http_5xx"] += 1
                if retries < self.max_retries:
                    self.metrics["web_fetch_retry_count"] += 1
                    if self.backoff_seconds:
                        self.sleep(self.backoff_seconds * (2 ** retries))
                    retries += 1
                    continue
            return response


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
            retry_rate_limit=True,
        )
        transport = MeteredTransport(PinnedTransport(), max_retries=config.retry_count)
        fetcher = SafeFetcher(transport=transport, timeout=config.read_timeout_seconds)
        clinic = dict(claim)
        clinic["city"] = clinic.get("city") or extract_municipality(clinic.get("address") or "")
        trusted = trusted_candidate_provider(clinic) if trusted_candidate_provider else ()
        return research_clinic(clinic, fetcher=fetcher, search_provider=provider,
                               trusted_candidates=trusted)
    return research_one
