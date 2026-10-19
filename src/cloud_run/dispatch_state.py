"""Durable record of the last Cloud Run dispatch outcome per HP research job id.

Keyed into the existing ``app_config.settings`` table -- no new schema object,
same pattern as ``src.repository.hp_auto_run_repository.SupabaseHpAutoRunRepository``.

Why this needs to be in the DB, not Streamlit ``session_state``: Cloud Run Web
runs with min instances = 0, so the container (and every session_state) can be
gone by the time a human comes back to check on a DISPATCH_UNKNOWN outcome.
The job's own existence/status in ``research_jobs`` is already durable, but
whether an *Execution actually started* for it is a fact this module -- not
that table -- is responsible for remembering, so a restarted container still
refuses to blindly re-dispatch.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

KEY_PREFIX = "hp_cloud_dispatch:"

STATUS_DISPATCHED = "DISPATCHED"
STATUS_DISPATCH_UNKNOWN = "DISPATCH_UNKNOWN"
STATUS_CONFIRMED_RUNNING = "CONFIRMED_RUNNING"
STATUS_SAFE_TO_RETRY = "SAFE_TO_RETRY"
STATUS_REJECTED = "REJECTED"

# A fresh dispatch for the same job id is only ever safe to attempt from one
# of these recorded states (or no recorded state at all).
RETRYABLE_STATUSES = frozenset({STATUS_SAFE_TO_RETRY, STATUS_REJECTED})


def _key(job_id):
    return KEY_PREFIX + str(job_id)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _decode(row):
    if not row:
        return None
    value = row[0]
    return value if isinstance(value, dict) else json.loads(value)


def record_dispatch_state(conn, job_id, *, status, operation_name=None, execution_name=None, detail=""):
    """Persist one dispatch/reconciliation outcome. Call this for every
    dispatch_hp_job() result (not only DISPATCH_UNKNOWN) so a later read can
    distinguish "never dispatched" from "confirmed running" from "still
    unknown"."""
    payload = {
        "status": status,
        "operation_name": operation_name,
        "execution_name": execution_name,
        "detail": str(detail or "")[:200],
        "recorded_at": _now(),
    }
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app_config.settings(key,value) VALUES(%s,%s) "
            "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
            (_key(job_id), json.dumps(payload, ensure_ascii=False)),
        )
    conn.commit()
    return payload


def get_dispatch_state(conn, job_id):
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM app_config.settings WHERE key=%s", (_key(job_id),))
        state = _decode(cur.fetchone())
    conn.commit()
    return state


def may_dispatch(conn, job_id):
    """True if there is no recorded dispatch yet, or the last recorded
    outcome is one a fresh dispatch is actually safe to follow. A job stuck
    at DISPATCH_UNKNOWN (unreconciled) or CONFIRMED_RUNNING must return False."""
    state = get_dispatch_state(conn, job_id)
    if state is None:
        return True
    return state.get("status") in RETRYABLE_STATUSES


def reconcile_pending_dispatch(conn, job_id, *, get_operation_fn):
    """Resolve a DISPATCH_UNKNOWN outcome by polling its Operation. Never
    starts a new Execution. No-op (returns the stored state unchanged) if
    there is nothing pending to reconcile."""
    from src.cloud_run.job_dispatch import (
        RECONCILE_FAILED, RECONCILE_RUNNING, RECONCILE_STILL_UNKNOWN, reconcile_operation,
    )

    state = get_dispatch_state(conn, job_id)
    if not state or state.get("status") != STATUS_DISPATCH_UNKNOWN:
        return state
    outcome = reconcile_operation(get_operation_fn=get_operation_fn, operation_name=state.get("operation_name"))
    if outcome == RECONCILE_STILL_UNKNOWN:
        return state
    resolved_status = STATUS_CONFIRMED_RUNNING if outcome == RECONCILE_RUNNING else STATUS_SAFE_TO_RETRY
    return record_dispatch_state(
        conn, job_id, status=resolved_status,
        operation_name=state.get("operation_name"), execution_name=state.get("execution_name"),
        detail=state.get("detail", ""),
    )
