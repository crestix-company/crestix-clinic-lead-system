"""Dedicated, least-privilege DB connection routing for the Cloud Run Google
Maps import Writer path (src.master.maps_cloud_import / app_v2.cloud_maps_
import_ui), mirroring src.cloud_run.comdesk_import_writer's proven pattern
exactly (which itself mirrors src.cloud_run.job_control).

Fail-closed, no implicit fallback: SUPABASE_MAPS_IMPORT_WRITER_DB_URL must be
set explicitly for cloud_maps_import_ui() to do anything. This module never
silently reuses SUPABASE_RUNTIME_DB_URL as a substitute. Until a human
provisions a dedicated, least-privilege Postgres role scoped to Google Maps
import writes and sets this variable, the Cloud Maps import path is unusable
by design -- same shape as job_control.py's HP Job role separation and
comdesk_import_writer.py's Comdesk role separation. This module itself never
creates, grants, or alters any database role or runs any migration/DDL.
"""
from __future__ import annotations

import os

from src.repository.write_backend import WRITE_BACKEND_SUPABASE
from src.repository.write_backend import build_write_repositories as _build_write_repositories

MAPS_IMPORT_WRITER_DB_URL_ENV_VAR = "SUPABASE_MAPS_IMPORT_WRITER_DB_URL"
RUNTIME_DB_URL_ENV_VAR = "SUPABASE_RUNTIME_DB_URL"


class MapsImportWriterNotConfiguredError(RuntimeError):
    pass


def maps_import_writer_db_url(env=None):
    env = os.environ if env is None else env
    url = (env.get(MAPS_IMPORT_WRITER_DB_URL_ENV_VAR) or "").strip()
    if not url:
        raise MapsImportWriterNotConfiguredError(
            f"{MAPS_IMPORT_WRITER_DB_URL_ENV_VAR}が未設定です。Cloud Run Google Maps取込では"
            f"一般DB接続（{RUNTIME_DB_URL_ENV_VAR}）への暗黙フォールバックを行いません。"
            "Google Maps取込専用の最小権限DBロール・接続URLを人間が明示的に設定してください。"
        )
    return url


def maps_import_writer_repositories(env=None, *, build_write_repositories=None):
    """Return a WriteRepositories bundle connected with the dedicated Google
    Maps import writer URL. Callers must not reuse this connection for
    unrelated writes -- those stay on the general write backend via
    write_repositories_for(). The same connection is also used, read-only
    (SELECT-only prefetch, always rolled back, never committed), for the
    matching-outcome preview in src.master.maps_cloud_import.preview_maps_import --
    not a second, separately-provisioned reader connection, since the
    preview reuses the exact same least-privilege role and the exact same
    matching queries the real write later runs."""
    url = maps_import_writer_db_url(env)
    builder = build_write_repositories or _build_write_repositories
    return builder(WRITE_BACKEND_SUPABASE, supabase_url=url)
