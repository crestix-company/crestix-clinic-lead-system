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
Google API client is constructed lazily, only inside default_execute_fn()'s
returned closure, never at import time or by any other function here.

Known open design question (not resolved by this module): which Supabase DB
role performs job creation/pause/resume when CLINIC_HP_EXECUTOR=cloud_run --
a dedicated least-privilege job-control role, or delegating job creation to
the Worker itself -- is explicitly left unresolved in the implementation plan
(PR B, "重要" paragraph) and requires a human decision plus (if a new role is
chosen) out-of-band Postgres role provisioning that this codebase does not
perform. This module reuses the existing CLINIC_WRITE_BACKEND/
SUPABASE_RUNTIME_DB_URL write path unchanged; it does not create, grant, or
assume any new role.
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


def dispatch_hp_job(job_id, *, env=None, execute_fn=None):
    """Start exactly one Execution for an already-created HP research job id.

    `job_id` must already exist as a research_jobs row (created the same way
    the local "指定件数だけ調査"/再開 path already creates/owns it) -- this
    function only ever overrides HP_WORKER_JOB_ID, never anything else.
    """
    from src.cloud_run.job_dispatch import run_hp_worker_job

    resource_name = job_resource_name(env)
    if execute_fn is None:
        execute_fn = default_execute_fn(env)
    return run_hp_worker_job(execute_fn=execute_fn, job_resource_name=resource_name, clinic_job_id=job_id)
