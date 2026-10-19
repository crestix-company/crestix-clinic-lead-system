"""Streamlit-side glue selecting between the local in-process HP runner and the
Cloud Run Jobs dispatch path, gated by CLINIC_HP_EXECUTOR.

Default (unset, or any value other than "cloud_run") is byte-for-byte the
existing local behavior: app_v2.py's simple_workflow_ui() keeps calling
runner.start()/auto_runner.start() exactly as before. Only an explicit
CLINIC_HP_EXECUTOR=cloud_run opts into delegating to src.cloud_run.job_dispatch.

Nothing in this module can reach GCP unless CLINIC_HP_EXECUTOR=cloud_run AND
CLOUD_RUN_PROJECT/CLOUD_RUN_REGION/CLOUD_RUN_HP_WORKER_JOB_NAME are all set AND
a human has separately deployed the Cloud Run Job (see
docs/cloud_run_on_demand_hp_implementation_plan.md, PR B/D). Importing or unit
testing this module never creates, calls, or bills any GCP resource: the
Google API client is constructed lazily, only inside default_execute_fn()'s/
default_get_operation_fn()'s returned closures, never at import time or by any
other function here.

What this module now enforces beyond a single API call (review follow-up on
PR #106):
- Job control (create/pause/resume) is routed through
  src.cloud_run.job_control's dedicated connection, kept separate from the
  general CLINIC_WRITE_BACKEND write path (see job_control.py for why).
- Every dispatch outcome is persisted via src.cloud_run.dispatch_state, and a
  new dispatch is refused outright (BLOCKED_PENDING_RECONCILE, no API call
  made) while a previous DISPATCH_UNKNOWN for the same job id has not been
  reconciled -- this survives a Cloud Run Web container restart (min
  instances = 0), unlike Streamlit session_state.
- Monthly execution/clinic-count budgets (src.cloud_run.usage_budget) are
  checked before every dispatch and before a new HP job is even created, so a
  budget refusal never leaves an orphaned, undispatchable job row behind.
"""
from __future__ import annotations

import os


LOCAL = "local"
CLOUD_RUN = "cloud_run"
VALID_MODES = (LOCAL, CLOUD_RUN)

EXECUTOR_ENV_VAR = "CLINIC_HP_EXECUTOR"
PROJECT_ENV_VAR = "CLOUD_RUN_PROJECT"
REGION_ENV_VAR = "CLOUD_RUN_REGION"
JOB_NAME_ENV_VAR = "CLOUD_RUN_HP_WORKER_JOB_NAME"

# PR D canary guidance: single-shot HP research is bounded to 25-50 clinics per
# Execution while CLINIC_HP_EXECUTOR=cloud_run. The unbounded "未調査がなくなる
# まで自動調査" mode stays local-only (see allow_unlimited_auto_run below).
CLOUD_RUN_MAX_SINGLE_JOB_COUNT = 50
LOCAL_MAX_SINGLE_JOB_COUNT = 500

# Dispatch outcomes decided entirely at this layer (never returned by
# src.cloud_run.job_dispatch itself, which only knows about one API call).
BLOCKED_PENDING_RECONCILE = "BLOCKED_PENDING_RECONCILE"
BUDGET_EXCEEDED = "BUDGET_EXCEEDED"


class CloudRunNotConfiguredError(RuntimeError):
    pass


def hp_executor_mode(env=None):
    env = os.environ if env is None else env
    value = (env.get(EXECUTOR_ENV_VAR) or LOCAL).strip().lower()
    if value not in VALID_MODES:
        raise ValueError(f"{EXECUTOR_ENV_VAR}は{VALID_MODES}のいずれかにしてください（指定値: {value!r}）。")
    return value


def is_cloud_run_executor(env=None):
    return hp_executor_mode(env) == CLOUD_RUN


def max_single_job_count(mode):
    return CLOUD_RUN_MAX_SINGLE_JOB_COUNT if mode == CLOUD_RUN else LOCAL_MAX_SINGLE_JOB_COUNT


def allow_unlimited_auto_run(mode):
    """"未調査がなくなるまで自動調査" is an unbounded, indefinitely-long-running
    controller (src.master.hp_auto_run.AutoHpRunner). Cloud Run Jobs here are
    always one bounded, explicitly user-triggered Execution per the implementation
    plan (PR B/C) -- so this mode is never offered once CLINIC_HP_EXECUTOR=cloud_run.
    """
    return mode == LOCAL


def job_resource_name(env=None):
    env = os.environ if env is None else env
    project = (env.get(PROJECT_ENV_VAR) or "").strip()
    region = (env.get(REGION_ENV_VAR) or "").strip()
    job_name = (env.get(JOB_NAME_ENV_VAR) or "").strip()
    if not (project and region and job_name):
        raise CloudRunNotConfiguredError(
            f"{PROJECT_ENV_VAR}/{REGION_ENV_VAR}/{JOB_NAME_ENV_VAR}が未設定です。"
            "Cloud Run Jobsの実デプロイ・設定は別途人間が行う必要があります。"
        )
    return f"projects/{project}/locations/{region}/jobs/{job_name}"


def default_execute_fn(env=None):
    """Build the real, Google-authenticated Jobs API `run` caller.

    Only ever constructed when a caller explicitly asks for it (never at
    module import time, never as a side effect of reading hp_executor_mode()),
    so unit tests and CI never need google-auth credentials or network access
    to import or exercise the rest of this module.
    """
    def execute_fn(request):
        from googleapiclient.discovery import build
        import google.auth

        credentials, _ = google.auth.default()
        client = build("run", "v2", credentials=credentials, cache_discovery=False)
        return client.projects().locations().jobs().run(
            name=request["name"], body=request["body"]
        ).execute()

    return execute_fn


def default_get_operation_fn(env=None):
    """Build the real, Google-authenticated long-running Operations `get`
    caller, used only for reconcile_hp_job_dispatch() -- never to start a new
    Execution. Lazily constructed for the same reason as default_execute_fn()."""
    def get_operation_fn(operation_name):
        from googleapiclient.discovery import build
        import google.auth

        credentials, _ = google.auth.default()
        client = build("run", "v2", credentials=credentials, cache_discovery=False)
        return client.operations().get(name=operation_name).execute()

    return get_operation_fn


def _default_job_control_conn(env=None):
    from src.cloud_run.job_control import job_control_write_repositories

    repositories = job_control_write_repositories(env)
    return repositories.jobs._conn


def precheck_budget(requested_clinic_count, *, env=None, job_control_conn=None):
    """Read-only, advisory-only budget check, meant to run BEFORE a new HP
    research job row is even created -- a budget refusal must never leave an
    orphaned, undispatchable job behind. Returns (allowed, reason).

    This is UX only, not the enforcement boundary: under concurrent calls two
    sessions could both pass this check before either reserves anything. The
    actual, race-safe enforcement happens inside dispatch_hp_job() via
    usage_budget.reserve_dispatch_usage(), which this function intentionally
    does not call (reserving budget for a job that might still fail to be
    created, e.g. due to a race on "no other HP job active", would need its
    own compensating release and is not worth the complexity for a UX-only
    precheck).
    """
    from src.cloud_run.usage_budget import check_budget

    conn = job_control_conn or _default_job_control_conn(env)
    return check_budget(conn, requested_clinic_count, env=env)


def dispatch_hp_job(job_id, *, clinic_count=0, env=None, execute_fn=None, job_control_conn=None):
    """Start exactly one Execution for an already-created HP research job id.

    `job_id` must already exist as a research_jobs row (created the same way
    the local "指定件数だけ調査"/再開 path already creates/owns it) -- this
    function only ever overrides HP_WORKER_JOB_ID, never anything else.

    `clinic_count` is the number of *new* clinics this dispatch call commits
    to the monthly budget: pass the job's size when first dispatching a freshly
    created job, and 0 when merely resuming an already-budgeted job (resuming
    does not add new clinics, only a new Execution attempt).

    Every call persists its outcome via src.cloud_run.dispatch_state,
    regardless of whether the Jobs API was reached at all -- BLOCKED_PENDING_
    RECONCILE and BUDGET_EXCEEDED are both final, visible outcomes callers
    must not silently retry.
    """
    from src.cloud_run.dispatch_state import may_dispatch, record_dispatch_state
    from src.cloud_run.job_dispatch import REJECTED, DispatchResult, run_hp_worker_job
    from src.cloud_run.usage_budget import release_dispatch_usage, reserve_dispatch_usage

    # Cheap, I/O-free config validation first: an unconfigured deployment must
    # fail with a clear config error before this ever opens a DB connection.
    resource_name = job_resource_name(env)

    conn = job_control_conn or _default_job_control_conn(env)

    if not may_dispatch(conn, job_id):
        return DispatchResult(status=BLOCKED_PENDING_RECONCILE, detail=(
            "前回の起動結果が未確認（DISPATCH_UNKNOWN）のまま残っています。"
            "照合（reconcile_hp_job_dispatch）を先に行ってください。再発行は行いません。"
        ))

    # Atomic check-and-reserve (not a plain read-then-write): this is what
    # actually prevents the monthly budget from being exceeded by concurrent
    # dispatches/double-clicks -- see usage_budget.reserve_dispatch_usage().
    allowed, reason = reserve_dispatch_usage(conn, clinic_count, env=env)
    if not allowed:
        return DispatchResult(status=BUDGET_EXCEEDED, detail=reason)

    if execute_fn is None:
        execute_fn = default_execute_fn(env)
    result = run_hp_worker_job(execute_fn=execute_fn, job_resource_name=resource_name, clinic_job_id=job_id)

    if result.status == REJECTED:
        # Nothing was started -- give the reservation back. Never done for
        # DISPATCH_UNKNOWN: an ambiguous outcome must stay counted (fail safe
        # toward under-counting remaining budget, never toward exceeding it).
        release_dispatch_usage(conn, clinic_count)

    record_dispatch_state(
        conn, job_id, status=result.status,
        operation_name=result.operation_name, execution_name=result.execution_name, detail=result.detail,
    )
    return result


def reconcile_hp_job_dispatch(job_id, *, env=None, get_operation_fn=None, job_control_conn=None):
    """Resolve a previously-DISPATCH_UNKNOWN outcome by polling its Operation.
    Never starts a new Execution. No-op if nothing is pending for this job id."""
    from src.cloud_run.dispatch_state import reconcile_pending_dispatch

    conn = job_control_conn or _default_job_control_conn(env)
    if get_operation_fn is None:
        get_operation_fn = default_get_operation_fn(env)
    return reconcile_pending_dispatch(conn, job_id, get_operation_fn=get_operation_fn)


def get_dispatch_state(job_id, *, env=None, job_control_conn=None):
    """Read-only lookup of the last recorded dispatch outcome, for UI display."""
    from src.cloud_run.dispatch_state import get_dispatch_state as _get_dispatch_state

    conn = job_control_conn or _default_job_control_conn(env)
    return _get_dispatch_state(conn, job_id)
