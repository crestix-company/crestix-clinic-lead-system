"""Dedicated, least-privilege DB connection routing for HP Job create/pause/
resume under CLINIC_HP_EXECUTOR=cloud_run.

Concretizes the role-separation question the implementation plan left open
(docs/cloud_run_on_demand_hp_implementation_plan.md, PR B, "重要" paragraph):
Cloud Run Web's general write backend (CLINIC_WRITE_BACKEND/
SUPABASE_RUNTIME_DB_URL -- used by e.g. refresh_age_model()/Comdesk import/
Google Maps import) and the HP Job create/pause/resume path are routed
through separate connection strings, so a human can provision two genuinely
distinct, least-privilege Postgres roles and point each env var at its own
role.

Fail-closed, no implicit fallback (review requirement): SUPABASE_JOB_CONTROL_DB_URL
must be set explicitly whenever CLINIC_HP_EXECUTOR=cloud_run. This module will
never silently reuse SUPABASE_RUNTIME_DB_URL (the general write connection) as
a substitute -- an earlier version of this module did exactly that, which
defeated the whole point of having a separate, least-privilege job-control
role. Until a human provisions the dedicated role and sets this variable, the
entire CLINIC_HP_EXECUTOR=cloud_run job-control path (create/pause/resume) is
unusable by design; see docs/cloud_run_job_control_db_role_design.md for the
exact privileges that role needs. This module itself never creates, grants,
or alters any database role or runs any migration/DDL.
"""
from __future__ import annotations

import os

from src.repository.write_backend import WRITE_BACKEND_SUPABASE
from src.repository.write_backend import build_write_repositories as _build_write_repositories

JOB_CONTROL_DB_URL_ENV_VAR = "SUPABASE_JOB_CONTROL_DB_URL"
RUNTIME_DB_URL_ENV_VAR = "SUPABASE_RUNTIME_DB_URL"


class JobControlNotConfiguredError(RuntimeError):
    pass


def job_control_db_url(env=None):
    env = os.environ if env is None else env
    url = (env.get(JOB_CONTROL_DB_URL_ENV_VAR) or "").strip()
    if not url:
        raise JobControlNotConfiguredError(
            f"{JOB_CONTROL_DB_URL_ENV_VAR}が未設定です。Cloud Runモードでは"
            f"一般DB接続（{RUNTIME_DB_URL_ENV_VAR}）への暗黙フォールバックを行いません。"
            "HP Job制御専用の最小権限DBロール・接続URLを人間が明示的に設定してください"
            "（docs/cloud_run_job_control_db_role_design.md参照）。"
        )
    return url


def job_control_write_repositories(env=None, *, build_write_repositories=None):
    """Return a WriteRepositories bundle connected with the job-control URL.

    Only `.jobs` and `.auto_hp` on the returned bundle are intended to be used
    from the CLINIC_HP_EXECUTOR=cloud_run dispatch path -- callers must not
    reuse this connection for unrelated writes (those stay on the general
    write backend via write_repositories_for()).
    """
    url = job_control_db_url(env)
    builder = build_write_repositories or _build_write_repositories
    return builder(WRITE_BACKEND_SUPABASE, supabase_url=url)
