"""Cloud Run Jobs entry point for one-shot HP research (clinic-lead-hp-worker).

Runs entirely synchronously inside a single Cloud Run Job task: no HTTP server,
no daemon Thread, no Streamlit. It resolves exactly one existing research job id
(HP_WORKER_JOB_ID, set as a containerOverride by src.cloud_run.job_dispatch) and
reuses the existing src.master.jobs.run_job() HP research loop unchanged.

Safety properties required by the implementation plan
(docs/cloud_run_on_demand_hp_implementation_plan.md, PR B):
- tasks=1 / parallelism=1 / max_retries=0 is the *intended* Job configuration,
  but this script defensively refuses to run if Cloud Run ever hands it a
  non-zero task index or a retried attempt (CLOUD_RUN_TASK_ATTEMPT != "0").
- Cross-Execution double-run is prevented with a session-scoped Postgres
  advisory lock keyed by job id (pg_try_advisory_lock). Postgres releases this
  automatically if the connection drops -- a killed/timed-out Execution can
  never leave a stale lock behind for the next Execution to wait on forever.
- SIGTERM (Cloud Run's timeout/cancel signal) is trapped and turned into the
  existing Stopped()/requeue_claimed_item() path via run_job()'s
  progress_callback, so an in-flight clinic is safely requeued instead of
  left claimed-but-abandoned. If the job is left RUNNING afterward (i.e. it
  did not reach COMPLETED), the job is explicitly paused so Phase C's UI
  never mistakes a dead Execution for a live one.
- A COMPLETED or RESET job is never re-run from here (no silent re-research).

No DB write role, Secret Manager binding, or Cloud Run resource is created by
this script -- it only uses whatever CLINIC_WRITE_BACKEND/SUPABASE_RUNTIME_DB_URL
credentials the deployed Job container already has.
"""
from __future__ import annotations

import logging
import os
import re
import signal
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

JOB_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
ADVISORY_LOCK_KEY_PREFIX = "crestix:hp_cloud_worker:"
BLOCKED_STATUSES = frozenset({"COMPLETED", "RESET"})

log = logging.getLogger("cloud_hp_worker")


class WorkerConfigError(ValueError):
    """Fail-closed configuration/environment problem; never starts run_job()."""


def validate_execution_environment(env):
    """Return the validated HP_WORKER_JOB_ID, or raise WorkerConfigError."""
    task_index = (env.get("CLOUD_RUN_TASK_INDEX") or "0").strip()
    task_attempt = (env.get("CLOUD_RUN_TASK_ATTEMPT") or "0").strip()
    job_id = (env.get("HP_WORKER_JOB_ID") or "").strip()
    if task_index != "0":
        raise WorkerConfigError(
            f"tasks=1想定のためCLOUD_RUN_TASK_INDEX=0のみ許可します（実際: {task_index}）。"
        )
    if task_attempt != "0":
        raise WorkerConfigError(
            "max_retries=0想定のためCLOUD_RUN_TASK_ATTEMPT=0のみ許可します。"
            f"未確認Executionの再試行は行いません（実際: {task_attempt}）。"
        )
    if not JOB_ID_PATTERN.fullmatch(job_id):
        raise WorkerConfigError("HP_WORKER_JOB_IDが未設定、または既存Job IDの形式ではありません。")
    return job_id


def acquire_worker_lock(lock_conn, job_id):
    with lock_conn.cursor() as cur:
        cur.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s))",
            (ADVISORY_LOCK_KEY_PREFIX + job_id,),
        )
        return bool(cur.fetchone()[0])


def release_worker_lock(lock_conn, job_id):
    with lock_conn.cursor() as cur:
        cur.execute(
            "SELECT pg_advisory_unlock(hashtext(%s))",
            (ADVISORY_LOCK_KEY_PREFIX + job_id,),
        )


def run_worker(
    *,
    env,
    store,
    lock_conn,
    run_job_fn,
    job_status_fn,
    pause_job_fn,
    stop_event=None,
    install_signal_handler=True,
):
    """Execute exactly one HP research job id end to end. Returns a result dict.

    Every dependency is injected so this can be exercised in tests without a
    real database, Cloud Run environment, or network access.
    """
    job_id = validate_execution_environment(env)

    current = job_status_fn(store, job_id)
    if current is None:
        raise WorkerConfigError(f"Job {job_id} の履歴が見つかりません。")
    if current["status"] in BLOCKED_STATUSES:
        log.info("job_id=%s status=%s のため再調査しません。", job_id, current["status"])
        return {"job_id": job_id, "outcome": "SKIPPED_" + current["status"], "ran": False}

    if not acquire_worker_lock(lock_conn, job_id):
        log.info("job_id=%s は別Executionが既に処理中のため終了します。", job_id)
        return {"job_id": job_id, "outcome": "ALREADY_RUNNING_ELSEWHERE", "ran": False}

    stop_event = stop_event or threading.Event()
    previous_handler = None
    try:
        if install_signal_handler:
            previous_handler = signal.signal(signal.SIGTERM, lambda *_args: stop_event.set())
        try:
            run_job_fn(store, job_id, progress_callback=lambda: not stop_event.is_set())
        finally:
            if install_signal_handler and previous_handler is not None:
                signal.signal(signal.SIGTERM, previous_handler)

        final = job_status_fn(store, job_id)
        if final["status"] == "RUNNING":
            # run_job() returned without reaching COMPLETED: SIGTERM/timeout cut it
            # short. The in-flight item was already safely requeued by run_job()'s
            # own Stopped()/requeue_claimed_item() path; persist PAUSED so a stale
            # RUNNING status never outlives this dead Execution.
            pause_job_fn(store, job_id)
            return {"job_id": job_id, "outcome": "INTERRUPTED_PAUSED", "ran": True}
        return {"job_id": job_id, "outcome": final["status"], "ran": True}
    finally:
        release_worker_lock(lock_conn, job_id)


def main(argv=None):
    logging.basicConfig(level=logging.INFO)
    from src.enrichment.search_provider import TavilySearchProvider
    from src.master.jobs import job_status, pause_job, run_job
    from src.repository.connection_diagnostics import safe_connection_message
    from src.repository.runtime_store import SupabaseRuntimeStore
    from src.repository.supabase_adapter import connect

    env = os.environ
    db_url = (env.get("SUPABASE_RUNTIME_DB_URL") or "").strip()
    if not db_url:
        print("SUPABASE_RUNTIME_DB_URL is not set", file=sys.stderr)
        return 1

    try:
        store = SupabaseRuntimeStore()
    except Exception as exc:
        print(safe_connection_message(exc), file=sys.stderr)
        return 1

    lock_conn = connect(db_url, autocommit=True)
    try:
        result = run_worker(
            env=env,
            store=store,
            lock_conn=lock_conn,
            run_job_fn=lambda s, jid, progress_callback: run_job(
                s, jid, TavilySearchProvider(""), progress_callback=progress_callback
            ),
            job_status_fn=job_status,
            pause_job_fn=pause_job,
        )
    except WorkerConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        lock_conn.close()

    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
