"""Bounded Cloud Run Jobs execution for the existing Email Research queue.

The Web process only creates/updates durable runs and dispatches a Job. Research
claims, candidate decisions and Primary Email selection stay in the existing
EmailAutoRunner and repositories. No GCP client is built until dispatch is
explicitly requested.
"""
from __future__ import annotations

import os

from src.master.email_auto_run import build_default_research_one
from src.master.email_research_config import load_email_research_config
from src.repository.email_database import build_email_repositories
from src.repository.email_target_reader import connect_email_target_reader

EXECUTOR_ENV = "EMAIL_AUTO_EXECUTOR"
JOB_NAME_ENV = "CLOUD_RUN_EMAIL_WORKER_JOB_NAME"
PROJECT_ENV = "CLOUD_RUN_PROJECT"
REGION_ENV = "CLOUD_RUN_REGION"
MAX_CLINICS_ENV = "EMAIL_CLOUD_RUN_MAX_CLINICS_PER_EXECUTION"
DEFAULT_MAX_CLINICS = 25
ABSOLUTE_MAX_CLINICS = 50


class CloudEmailNotConfiguredError(RuntimeError):
    pass


class EmailDispatchOutcomeUnknownError(CloudEmailNotConfiguredError):
    """The Run exists, but the Jobs API response did not prove Execution creation."""


def email_cloud_run_enabled(env=None):
    env = os.environ if env is None else env
    return (env.get(EXECUTOR_ENV) or "").strip().lower() == "cloud_run"


def max_clinics_per_execution(env=None):
    env = os.environ if env is None else env
    raw = (env.get(MAX_CLINICS_ENV) or str(DEFAULT_MAX_CLINICS)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise CloudEmailNotConfiguredError(f"{MAX_CLINICS_ENV}は1〜{ABSOLUTE_MAX_CLINICS}の整数が必要です。") from exc
    if not 1 <= value <= ABSOLUTE_MAX_CLINICS:
        raise CloudEmailNotConfiguredError(f"{MAX_CLINICS_ENV}は1〜{ABSOLUTE_MAX_CLINICS}の範囲で設定してください。")
    return value


def job_resource_name(env=None):
    env = os.environ if env is None else env
    project = (env.get(PROJECT_ENV) or "").strip()
    region = (env.get(REGION_ENV) or "").strip()
    job = (env.get(JOB_NAME_ENV) or "").strip()
    if not (project and region and job):
        raise CloudEmailNotConfiguredError(
            f"{PROJECT_ENV}/{REGION_ENV}/{JOB_NAME_ENV}が必要です。Email Worker Jobの設定がありません。"
        )
    return f"projects/{project}/locations/{region}/jobs/{job}"


def _repositories():
    bundle, status = build_email_repositories()
    if bundle is None or bundle.email_research is None:
        raise CloudEmailNotConfiguredError(status.message)
    return bundle


def create_email_run(*, env=None, repositories_factory=None, target_reader=None, config=None):
    """Sync the eligible HP-verified URL cohort and create a durable Auto Run."""
    env = os.environ if env is None else env
    # Validate the target Job and bounded execution before creating durable state.
    job_resource_name(env)
    max_clinics_per_execution(env)
    bundle = (repositories_factory or _repositories)()
    repo = bundle.email_research
    if not repo.available():
        raise CloudEmailNotConfiguredError("Email Research schemaが利用できません。")
    reader = target_reader
    if reader is None:
        reader = connect_email_target_reader()
        close_reader = True
    else:
        close_reader = False
    try:
        targets = reader.eligible_targets()
        repo.sync_targets(targets)
        cfg = config or load_email_research_config(env)
        return repo.create_auto_run(
            workers=cfg.workers, timeout=cfg.read_timeout_seconds,
            retries=cfg.retry_count, search_limit=cfg.search_query_limit,
        )
    finally:
        if close_reader:
            reader._conn.close()
        if hasattr(repo, "close"):
            repo.close()


def dispatch_email_worker(run_id, *, env=None, execute_fn=None):
    """Dispatch one bounded execution. Ambiguous outcomes are surfaced for operator review."""
    env = os.environ if env is None else env
    name = job_resource_name(env)
    max_clinics_per_execution(env)
    if execute_fn is None:
        def execute_fn(request):
            import google.auth
            from googleapiclient.discovery import build
            credentials, _ = google.auth.default()
            client = build("run", "v2", credentials=credentials, cache_discovery=False)
            return client.projects().locations().jobs().run(name=request["name"], body=request["body"]).execute()
    request = {
        "name": name,
        "body": {"overrides": {"containerOverrides": [{"env": [
            {"name": "EMAIL_WORKER_RUN_ID", "value": str(run_id)},
            {"name": MAX_CLINICS_ENV, "value": str(max_clinics_per_execution(env))},
        ]}] }},
    }
    try:
        response = execute_fn(request)
    except Exception as exc:
        # The worker uses the durable controller lease; a duplicate execution will
        # become standby. Do not automatically retry an ambiguous Jobs API call.
        raise EmailDispatchOutcomeUnknownError(
            "Email Worker起動結果を確認できません。自動再起動は行わず、Run状態とCloud Run Executionを照合してください。"
        ) from exc
    if not isinstance(response, dict) or not (response.get("name") or response.get("metadata", {}).get("name")):
        raise EmailDispatchOutcomeUnknownError("Cloud Run Execution名を確認できません。自動再起動は行いません。")
    return response


def update_run(run_id, status, *, repositories_factory=None):
    bundle = (repositories_factory or _repositories)()
    try:
        return bundle.email_research.update_auto_run(run_id, status=status,
                                                     stop_requested=(status == "STOPPED"))
    finally:
        if hasattr(bundle.email_research, "close"):
            bundle.email_research.close()


def build_worker_runner(*, repositories_factory=None, research_one=None, config=None,
                        env=None):
    """Construct a worker with a fixed, already-synced cohort (no continual target refresh)."""
    from src.master.email_auto_run import EmailAutoRunner
    env = os.environ if env is None else env
    maximum = max_clinics_per_execution(env)
    factory = repositories_factory or (lambda: _repositories().email_research)
    return EmailAutoRunner(
        factory, research_one or build_default_research_one(), target_reader=None,
        config=config or load_email_research_config(env),
        max_claims_per_execution=maximum,
    )
