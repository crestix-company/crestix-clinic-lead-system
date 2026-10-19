"""Dedicated, least-privilege DB connection routing for HP Job create/pause/
resume under CLINIC_HP_EXECUTOR=cloud_run.

Concretizes the role-separation question the implementation plan left open
(docs/cloud_run_on_demand_hp_implementation_plan.md, PR B, "重要" paragraph):
Cloud Run Web's general write backend (CLINIC_WRITE_BACKEND/
SUPABASE_RUNTIME_DB_URL -- used by e.g. refresh_age_model()/Comdesk import/
Google Maps import) and the HP Job create/pause/resume path are now routed
through separate connection strings, so a human can provision two genuinely
distinct, least-privilege Postgres roles and point each env var at its own
role without any further code change.

This module does not create, grant, or alter any database role or run any
migration/DDL -- provisioning SUPABASE_JOB_CONTROL_DB_URL's actual role is a
separate, human infra action. Until that role exists, SUPABASE_JOB_CONTROL_DB_URL
stays unset and this falls back to SUPABASE_RUNTIME_DB_URL, i.e. today's
behavior (job control shares the general write connection) is unchanged.
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
    url = (env.get(JOB_CONTROL_DB_URL_ENV_VAR) or env.get(RUNTIME_DB_URL_ENV_VAR) or "").strip()
    if not url:
        raise JobControlNotConfiguredError(
            f"{JOB_CONTROL_DB_URL_ENV_VAR}（未設定時は{RUNTIME_DB_URL_ENV_VAR}へフォールバック）が必要です。"
        )
    return url


def using_dedicated_job_control_role(env=None):
    """True only once a human has actually provisioned a distinct role and set
    SUPABASE_JOB_CONTROL_DB_URL -- used purely for diagnostics/UI messaging,
    never to change which checks run."""
    env = os.environ if env is None else env
    return bool((env.get(JOB_CONTROL_DB_URL_ENV_VAR) or "").strip())


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
