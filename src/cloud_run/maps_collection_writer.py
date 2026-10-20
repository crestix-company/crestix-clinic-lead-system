"""Dedicated, least-privilege DB connection routing for the Cloud Run Google
Maps Collection management screen (app_v2.cloud_maps_collection_admin_ui),
mirroring src.cloud_run.maps_import_writer / comdesk_import_writer's proven
pattern exactly.

This connection is used for collection-task STATE ONLY (create/assign/hold/
resume/release/fail/complete, see src.repository.maps_collection_tasks_repository)
-- never for the clinic-record writes src.master.google_maps.
import_maps_results() performs. Those stay on the separate, already-
established src.cloud_run.maps_import_writer connection. Keeping them
distinct means a role compromised or misconfigured for one of these two
features cannot affect the other's data at all.

Fail-closed, no implicit fallback: SUPABASE_MAPS_COLLECTION_WRITER_DB_URL
must be set explicitly for cloud_maps_collection_admin_ui() to do anything.
This module never silently reuses SUPABASE_RUNTIME_DB_URL as a substitute.
Until a human provisions a dedicated, least-privilege Postgres role scoped
to this feature and sets this variable, the Cloud Collection management
screen is unusable by design. This module itself never creates, grants, or
alters any database role or runs any migration/DDL -- and no migration/DDL
is needed at all: the task list is stored as one JSON value in the existing
app_config.settings table (see the repository module's own docstring), the
same no-new-schema-object approach src.repository.hp_auto_run_repository
already uses for HP auto-run state.
"""
from __future__ import annotations

import os

MAPS_COLLECTION_WRITER_DB_URL_ENV_VAR = "SUPABASE_MAPS_COLLECTION_WRITER_DB_URL"
RUNTIME_DB_URL_ENV_VAR = "SUPABASE_RUNTIME_DB_URL"


class MapsCollectionWriterNotConfiguredError(RuntimeError):
    pass


def maps_collection_writer_db_url(env=None):
    env = os.environ if env is None else env
    url = (env.get(MAPS_COLLECTION_WRITER_DB_URL_ENV_VAR) or "").strip()
    if not url:
        raise MapsCollectionWriterNotConfiguredError(
            f"{MAPS_COLLECTION_WRITER_DB_URL_ENV_VAR}が未設定です。Cloud Run Google Maps収集管理では"
            f"一般DB接続（{RUNTIME_DB_URL_ENV_VAR}）への暗黙フォールバックを行いません。"
            "収集タスク管理専用の最小権限DBロール・接続URLを人間が明示的に設定してください。"
        )
    return url


def maps_collection_tasks_repository(env=None, *, connect=None, repository_cls=None):
    """Return a SupabaseMapsCollectionTasksRepository connected with the
    dedicated collection-task writer URL. This repository is NOT part of
    the general WriteRepositories bundle (it has nothing to do with clinic
    records) -- it owns its own connection directly, same shape as
    src.cloud_run.job_control's job-control connection."""
    from src.repository.maps_collection_tasks_repository import (
        SupabaseMapsCollectionTasksRepository,
    )
    from src.repository.supabase_adapter import connect as _connect

    url = maps_collection_writer_db_url(env)
    connector = connect or _connect
    cls = repository_cls or SupabaseMapsCollectionTasksRepository
    conn = connector(url)
    return cls(conn)
