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


def _current_job_status(conn, job_id):
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM research.research_jobs WHERE id=%s", (job_id,))
        row = cur.fetchone()
    conn.commit()
    return row[0] if row else None


def may_dispatch(conn, job_id):
    """True if a fresh dispatch for this job id is safe right now.

    Fix for the "stuck forever" bug: a recorded DISPATCHED/CONFIRMED_RUNNING
    outcome must NOT block the normal pause -> resume cycle permanently.
    Those statuses only mean "an Execution may still be live" -- the
    authoritative signal for whether it actually still is lives in the job's
    own research.research_jobs.status, which is set to RUNNING at creation
    and is the one thing every code path (the worker's own loop, an explicit
    "一時停止" via job_control, or SIGTERM-triggered safe pause in
    scripts/cloud_hp_worker.py) agrees on changing away from once no
    Execution is actively processing it. So:

    - DISPATCH_UNKNOWN / DISPATCHED / CONFIRMED_RUNNING only block while the
      job's own DB status is still RUNNING.
    - Once that status is PAUSED / BUDGET / COMPLETED / RESET (or the job
      cannot be found), that transition itself proves no Execution is
      currently live, and a fresh dispatch (typically "この調査を再開") must
      be allowed again, regardless of what this module's own last-recorded
      status says.
    - SAFE_TO_RETRY / REJECTED were already known-safe and remain so.
    """
    state = get_dispatch_state(conn, job_id)
    if state is None or state.get("status") in RETRYABLE_STATUSES:
        return True
    return _current_job_status(conn, job_id) != "RUNNING"


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
