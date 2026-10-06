"""Stage4-C runtime cutover: Supabase READ primary, SQLite WRITE primary.

Only methods explicitly implemented by :class:`Stage4CClinicStore` read from Supabase.  Every
other attribute and method delegates to the original ``ClinicStore`` instance, which keeps all
mutations (imports, research saves, overrides, review resolution, reintegration and settings)
on SQLite.  ``CLINIC_DATA_BACKEND=sqlite`` bypasses this wrapper for immediate rollback.

Primary failures and deliberately unsupported filters use a visible, counted SQLite fallback.
Successful primary reads may be compared with SQLite asynchronously; comparator work never
blocks the UI response and never changes the returned Supabase value.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, is_dataclass
from pathlib import Path

COMPARATOR_ENV_VAR = "CLINIC_READ_COMPARATOR_ENABLED"
STATEMENT_TIMEOUT_ENV_VAR = "CLINIC_SUPABASE_STATEMENT_TIMEOUT_MS"
DEFAULT_STATEMENT_TIMEOUT_MS = 10_000
_LOG_PATH = Path(__file__).resolve().parents[2] / "logs" / "read_cutover.log"


def comparator_enabled():
    return (os.environ.get(COMPARATOR_ENV_VAR) or "1").strip() == "1"


def _build_logger():
    logger = logging.getLogger("clinic.read_cutover")
    if not logger.handlers:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(_LOG_PATH, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


_logger = _build_logger()
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="sqlite-comparator")
_inflight = threading.Semaphore(2)


def _log(record):
    try:
        _logger.info(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str))
    except Exception:
        pass


def _fingerprint(operation, args, kwargs):
    def normalize(value):
        if is_dataclass(value):
            return asdict(value)
        if isinstance(value, (list, tuple)):
            return [normalize(v) for v in value]
        return value

    payload = json.dumps(
        [operation, normalize(args), normalize(kwargs)],
        ensure_ascii=False, sort_keys=True, default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _comparison_value(operation, value):
    if operation == "query":
        return [row["id"] for row in value]
    return value


class Stage4CClinicStore:
    """Drop-in ``ClinicStore`` facade with a strict READ/WRITE split."""

    READ_OPERATIONS = frozenset({
        "get", "get_by_uuid", "get_by_medical_key", "query", "count", "funnel",
        "metrics", "treatment_status_for_ids", "web_research_metrics",
    })

    def __init__(self, sqlite_store, *, primary_repositories=None):
        self._sqlite = sqlite_store
        self._primary_repositories = primary_repositories
        self._primary_lock = threading.Lock()
        self._fallback_count = 0
        self._fallback_lock = threading.Lock()

    def __getattr__(self, name):
        # All non-whitelisted calls, including every mutation, remain on SQLite.
        return getattr(self._sqlite, name)

    @property
    def fallback_count(self):
        with self._fallback_lock:
            return self._fallback_count

    def _repositories(self):
        if self._primary_repositories is not None:
            conn = getattr(self._primary_repositories.clinics, "_conn", None)
            if conn is None or not getattr(conn, "closed", False):
                return self._primary_repositories
        with self._primary_lock:
            if self._primary_repositories is None:
                from src.repository.backend import build_repositories
                repos = build_repositories("supabase")
                conn = repos.clinics._conn
                timeout_ms = int(os.environ.get(
                    STATEMENT_TIMEOUT_ENV_VAR, str(DEFAULT_STATEMENT_TIMEOUT_MS)
                ))
                with conn.cursor() as cur:
                    cur.execute(f"SET statement_timeout = {timeout_ms}")
                self._primary_repositories = repos
        return self._primary_repositories

    @staticmethod
    def _fallback_reason(exc):
        from src.repository.errors import BackendNotSupportedError
        if isinstance(exc, BackendNotSupportedError):
            return "unsupported_read_path"
        if isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower():
            return "timeout"
        if isinstance(exc, (ConnectionError, OSError)) or "operational" in type(exc).__name__.lower():
            return "connection_error"
        return "query_error"

    def _fallback(self, operation, reason, fingerprint, call):
        with self._fallback_lock:
            self._fallback_count += 1
            count = self._fallback_count
        _log({
            "event": "fallback", "operation": operation, "fallback_count": count,
            "fallback_reason": self._fallback_reason(reason),
            "error_category": type(reason).__name__, "fingerprint": fingerprint,
        })
        return call()

    def _compare_async(self, operation, fingerprint, primary_value, sqlite_call):
        if not comparator_enabled():
            return
        if not _inflight.acquire(blocking=False):
            _log({"event": "comparator_skipped_saturated", "operation": operation,
                  "fingerprint": fingerprint})
            return

        def task():
            started = time.monotonic()
            try:
                sqlite_value = sqlite_call()
                match = _comparison_value(operation, primary_value) == _comparison_value(
                    operation, sqlite_value
                )
                _log({
                    "event": "comparator", "operation": operation,
                    "fingerprint": fingerprint, "match": match,
                    "sqlite_latency_ms": round((time.monotonic() - started) * 1000, 1),
                })
            except Exception as exc:
                _log({
                    "event": "comparator_error", "operation": operation,
                    "fingerprint": fingerprint, "error_category": type(exc).__name__,
                })
            finally:
                _inflight.release()

        try:
            _executor.submit(task)
        except Exception:
            _inflight.release()

    def _read(self, operation, primary_call, sqlite_call, args=(), kwargs=None):
        kwargs = kwargs or {}
        fingerprint = _fingerprint(operation, args, kwargs)
        try:
            value = primary_call(self._repositories())
        except Exception as exc:
            # Unsupported Stage4-A fields are an intentional compatibility fallback; connection,
            # timeout and query errors follow the same safe path.  All are counted and logged.
            # Rebuild the Supabase connection on the next operation after any runtime failure.
            # Unsupported filters are deterministic and do not imply a broken connection.
            from src.repository.errors import BackendNotSupportedError
            if not isinstance(exc, BackendNotSupportedError):
                with self._primary_lock:
                    self._primary_repositories = None
            return self._fallback(operation, exc, fingerprint, sqlite_call)
        self._compare_async(operation, fingerprint, value, sqlite_call)
        return value

    def get(self, clinic_id):
        return self._read("get", lambda r: r.clinics.get(clinic_id),
                          lambda: self._sqlite.get(clinic_id), (clinic_id,))

    def get_by_uuid(self, uuid):
        return self._read("get_by_uuid", lambda r: r.clinics.get_by_uuid(uuid),
                          lambda: self._sqlite.get_by_uuid(uuid), (uuid,))

    def get_by_medical_key(self, medical_key):
        return self._read("get_by_medical_key", lambda r: r.clinics.get_by_medical_key(medical_key),
                          lambda: self._sqlite.get_by_medical_key(medical_key), (medical_key,))

    def query(self, filters=None, limit=100, offset=0, as_of=None):
        args = (filters, limit, offset, as_of)
        return self._read("query", lambda r: r.clinics.query(*args),
                          lambda: self._sqlite.query(*args), args)

    def count(self, filters=None, as_of=None):
        args = (filters, as_of)
        return self._read("count", lambda r: r.clinics.count(*args),
                          lambda: self._sqlite.count(*args), args)

    def funnel(self, filters, as_of=None):
        args = (filters, as_of)
        return self._read("funnel", lambda r: r.clinics.funnel(*args),
                          lambda: self._sqlite.funnel(*args), args)

    def metrics(self):
        return self._read("metrics", lambda r: r.clinics.metrics(), self._sqlite.metrics)

    def treatment_status_for_ids(self, ids):
        ids = list(ids)
        return self._read(
            "treatment_status_for_ids", lambda r: r.treatment.status_for_ids(ids),
            lambda: self._sqlite.treatment_status_for_ids(ids), (ids,),
        )

    def web_research_metrics(self):
        from src.master.hp_batch_metrics import web_research_metrics
        return self._read(
            "web_research_metrics", lambda r: r.hp_research.batch_metrics(),
            lambda: web_research_metrics(self._sqlite.path),
        )
