"""Stage4-B Shadow Read.

SQLite stays Primary: every method below calls the existing, unmodified ClinicStore first and
returns exactly that value to the caller, synchronously, before Supabase is ever touched. The
Supabase comparison runs afterwards in a bounded background thread and can only ever produce a
log line -- it can never change the return value, never raise into the caller, and never block
the caller beyond the time it took to get the primary (SQLite) result.

Enable with CLINIC_SHADOW_READ_ENABLED=1 (default: unset/0 == off). When off, app_v2.py's
store_for() never even imports this module, so the default code path is byte-for-byte what it
was before Stage4-B.
"""
import hashlib
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, is_dataclass
from pathlib import Path

from src.repository.errors import BackendNotSupportedError

SHADOW_ENV_VAR = "CLINIC_SHADOW_READ_ENABLED"
# Stage4-B.6 P0-1 note: the hospital/center excluded_ids cache (SupabaseClinicRepository._excluded_
# clinic_ids) costs ~4.2s the first time any count()/query()/funnel() runs on a given connection
# (one-time per thread-local connection, never repeated after) -- the timeout must comfortably
# exceed that, or that one query gets killed by statement_timeout every single time (never
# finishing, never caching) and -- since it runs on a non-autocommit connection -- the resulting
# QueryCanceled left every later query on that connection failing with InFailedSqlTransaction
# until the process restarted. Both found and fixed together: raised the default here, and
# _shadow_repos() below now opens its connection with autocommit=True so a failed/cancelled
# statement can never poison later statements on the same reused connection.
SHADOW_TIMEOUT_SECONDS = float(os.environ.get("CLINIC_SHADOW_READ_TIMEOUT_SECONDS", "10"))
SHADOW_MAX_WORKERS = int(os.environ.get("CLINIC_SHADOW_READ_MAX_WORKERS", "4"))

_LOG_PATH = Path(__file__).resolve().parents[2] / "logs" / "shadow_read.log"


def shadow_read_enabled():
    return (os.environ.get(SHADOW_ENV_VAR) or "0").strip() == "1"


def _build_logger():
    logger = logging.getLogger("clinic.shadow_read")
    if not logger.handlers:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(_LOG_PATH, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


_logger = _build_logger()
# Bounded: exactly SHADOW_MAX_WORKERS threads ever run shadow work, and the semaphore below
# refuses extra submissions outright (logged as "skipped_saturated") instead of queuing them --
# so load on the Supabase side, and memory for pending tasks, can never grow unbounded.
_executor = ThreadPoolExecutor(max_workers=SHADOW_MAX_WORKERS, thread_name_prefix="shadow-read")
_inflight = threading.Semaphore(SHADOW_MAX_WORKERS)
_thread_local = threading.local()


def _log(record):
    try:
        _logger.info(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str))
    except Exception:
        pass  # logging must never be able to break the caller


def fingerprint(*parts):
    """A short, non-reversible fingerprint of the query shape -- never the raw filter values
    (a keyword filter can contain a clinic name), never clinic/patient content.
    """
    normalized = [asdict(p) if is_dataclass(p) else p for p in parts]
    canonical = json.dumps(normalized, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _shadow_repos():
    """One Supabase connection per worker thread (at most SHADOW_MAX_WORKERS total -- the
    ThreadPoolExecutor never grows beyond that), lazily opened, auto-reconnected if dropped.
    A real pool (psycopg_pool) would be the production-grade version of this; for Stage4-B's
    bounded 4-worker shadow traffic, one connection per worker thread is simpler and sufficient.
    """
    cached = getattr(_thread_local, "repos", None)
    if cached is not None and not cached[-1].closed:
        return cached[:-1]
    from src.repository.supabase_adapter import (
        connect, SupabaseClinicRepository, SupabaseHpResearchRepository, SupabaseTreatmentRepository,
    )
    url = os.environ.get("SUPABASE_DB_URL")
    if not url:
        raise RuntimeError("SUPABASE_DB_URL is not set")
    # autocommit=True: without this, a cancelled/failed statement (e.g. a statement_timeout hit)
    # left this reused connection in an open, aborted transaction -- every later statement on it
    # then failed with InFailedSqlTransaction until the process restarted (found via the shadow
    # log during Stage4-B.6 UI testing: 21 of 23 shadow attempts failed this way). With
    # autocommit, each statement is independent, so one failure can never poison the ones after
    # it. Must be passed into connect() itself -- psycopg refuses to flip autocommit after the
    # connection's first statement, which already runs inside connect().
    conn = connect(url, connect_timeout=5, autocommit=True)
    with conn.cursor() as cur:
        # Server-side bound: a hung/slow shadow query is killed by Postgres itself, so a stuck
        # shadow task can never occupy its worker slot indefinitely. SET does not support bind
        # parameters at all (psycopg.errors.SyntaxError: "syntax error at or near $1") -- the
        # value is our own int(), never user input, so a formatted literal is safe here.
        cur.execute(f"SET statement_timeout = {int(SHADOW_TIMEOUT_SECONDS * 1000)}")
    hp = SupabaseHpResearchRepository(conn)
    clinics = SupabaseClinicRepository(conn, hp)
    treatment = SupabaseTreatmentRepository(conn)
    _thread_local.repos = (clinics, hp, treatment, conn)
    return clinics, hp, treatment


# -- comparators: return (match: bool, mismatches: list[{"column","difference_type"}]) ------
def _compare_get(sqlite_value, supabase_value):
    if sqlite_value is None and supabase_value is None:
        return True, []
    if (sqlite_value is None) != (supabase_value is None):
        return False, [{"column": "<row>", "difference_type": "presence_mismatch"}]
    keys = sorted(set(sqlite_value) | set(supabase_value))
    diffs = [{"column": k, "difference_type": "value_mismatch"} for k in keys if sqlite_value.get(k) != supabase_value.get(k)]
    return (not diffs), diffs[:20]


def _compare_id_list(sqlite_value, supabase_value):
    if sqlite_value == supabase_value:
        return True, []
    return False, [{"column": "<id_list_order>", "difference_type": "value_mismatch"}]


def _compare_scalar(sqlite_value, supabase_value):
    if sqlite_value == supabase_value:
        return True, []
    return False, [{"column": "<value>", "difference_type": "value_mismatch"}]


def _compare_dict(sqlite_value, supabase_value):
    keys = sorted(set(sqlite_value) | set(supabase_value))
    diffs = [{"column": k, "difference_type": "value_mismatch"} for k in keys if sqlite_value.get(k) != supabase_value.get(k)]
    return (not diffs), diffs[:20]


def run_shadow(operation, query_fingerprint, clinic_id, shadow_fn, comparator, sqlite_result, sqlite_elapsed_seconds):
    """Fire-and-forget: submits the shadow comparison and returns immediately. Never raises."""
    if not shadow_read_enabled():
        return
    if not _inflight.acquire(blocking=False):
        _log({"op": operation, "fingerprint": query_fingerprint, "event": "skipped_saturated"})
        return

    def task():
        t0 = time.monotonic()
        try:
            supabase_result = shadow_fn()
            elapsed = time.monotonic() - t0
            match, mismatches = comparator(sqlite_result, supabase_result)
            record = {
                "op": operation, "fingerprint": query_fingerprint, "match": match,
                "sqlite_latency_ms": round(sqlite_elapsed_seconds * 1000, 1),
                "supabase_latency_ms": round(elapsed * 1000, 1),
            }
            if clinic_id is not None:
                record["clinic_id"] = clinic_id
            if not match:
                record["mismatches"] = mismatches
            _log(record)
        except BackendNotSupportedError as exc:
            _log({"op": operation, "fingerprint": query_fingerprint, "event": "skipped_unsupported",
                  "error_category": "BackendNotSupportedError", "detail": str(exc)[:200]})
        except Exception as exc:
            _log({"op": operation, "fingerprint": query_fingerprint, "event": "shadow_error",
                  "error_category": type(exc).__name__, "detail": str(exc)[:200]})
        finally:
            _inflight.release()

    try:
        _executor.submit(task)
    except Exception:
        _inflight.release()


class ShadowClinicStore:
    """Drop-in wrapper around ClinicStore. Unlisted attributes/methods pass straight through to
    the primary store unchanged (`__getattr__`), so wiring this in at the single `store_for()`
    factory call site is the only app_v2.py change Stage4-B needs.
    """

    def __init__(self, primary):
        self._primary = primary

    def __getattr__(self, name):
        return getattr(self._primary, name)

    def get(self, cid):
        t0 = time.monotonic()
        result = self._primary.get(cid)
        elapsed = time.monotonic() - t0
        run_shadow("get", fingerprint("get", cid), cid,
                   lambda: _shadow_repos()[0].get(cid), _compare_get, result, elapsed)
        return result

    def query(self, filters=None, limit=100, offset=0, as_of=None):
        t0 = time.monotonic()
        result = self._primary.query(filters, limit, offset, as_of)
        elapsed = time.monotonic() - t0
        ids = [rec["id"] for rec in result]

        def shadow_call():
            recs = _shadow_repos()[0].query(filters, limit, offset, as_of)
            return [rec["id"] for rec in recs]

        run_shadow("query", fingerprint("query", filters, limit, offset, as_of), None,
                   shadow_call, _compare_id_list, ids, elapsed)
        return result

    def count(self, filters=None, as_of=None):
        t0 = time.monotonic()
        result = self._primary.count(filters, as_of)
        elapsed = time.monotonic() - t0
        run_shadow("count", fingerprint("count", filters, as_of), None,
                   lambda: _shadow_repos()[0].count(filters, as_of), _compare_scalar, result, elapsed)
        return result

    def funnel(self, filters, as_of=None):
        t0 = time.monotonic()
        result = self._primary.funnel(filters, as_of)
        elapsed = time.monotonic() - t0
        run_shadow("funnel", fingerprint("funnel", filters, as_of), None,
                   lambda: _shadow_repos()[0].funnel(filters, as_of), _compare_scalar, result, elapsed)
        return result

    def metrics(self):
        t0 = time.monotonic()
        result = self._primary.metrics()
        elapsed = time.monotonic() - t0
        run_shadow("metrics", fingerprint("metrics"), None,
                   lambda: _shadow_repos()[0].metrics(), _compare_dict, result, elapsed)
        return result

    def treatment_status_for_ids(self, ids):
        t0 = time.monotonic()
        result = self._primary.treatment_status_for_ids(ids)
        elapsed = time.monotonic() - t0
        ids_list = list(ids)
        run_shadow("treatment_status_for_ids", fingerprint("treatment_status_for_ids", ids_list), None,
                   lambda: _shadow_repos()[2].status_for_ids(ids_list), _compare_dict, result, elapsed)
        return result
