"""Dedicated, least-privilege DB connection routing for the Cloud Run 厚生局
master import Writer path, mirroring src.cloud_run.job_control and
src.cloud_run.comdesk_import_writer exactly (see either for the full
reasoning -- not repeated here): fail-closed, no implicit fallback to the
general write connection. SUPABASE_KOUSEIKYOKU_IMPORT_WRITER_DB_URL must be
set explicitly for app_v2.cloud_kouseikyoku_import_ui() to do anything.
This module never creates, grants, or alters any database role or runs any
migration/DDL.
"""
from __future__ import annotations

import os

from src.repository.write_backend import WRITE_BACKEND_SUPABASE
from src.repository.write_backend import build_write_repositories as _build_write_repositories

KOUSEIKYOKU_IMPORT_WRITER_DB_URL_ENV_VAR = "SUPABASE_KOUSEIKYOKU_IMPORT_WRITER_DB_URL"
RUNTIME_DB_URL_ENV_VAR = "SUPABASE_RUNTIME_DB_URL"


class KouseikyokuImportWriterNotConfiguredError(RuntimeError):
    pass


def kouseikyoku_import_writer_db_url(env=None):
    env = os.environ if env is None else env
    url = (env.get(KOUSEIKYOKU_IMPORT_WRITER_DB_URL_ENV_VAR) or "").strip()
    if not url:
        raise KouseikyokuImportWriterNotConfiguredError(
            f"{KOUSEIKYOKU_IMPORT_WRITER_DB_URL_ENV_VAR}が未設定です。Cloud Run厚生局取込では"
            f"一般DB接続（{RUNTIME_DB_URL_ENV_VAR}）への暗黙フォールバックを行いません。"
            "厚生局取込専用の最小権限DBロール・接続URLを人間が明示的に設定してください。"
        )
    return url


def kouseikyoku_import_writer_repositories(env=None, *, build_write_repositories=None):
    """Return a WriteRepositories bundle connected with the dedicated
    厚生局 import writer URL. Callers must not reuse this connection for
    unrelated writes -- those stay on the general write backend via
    write_repositories_for()."""
    url = kouseikyoku_import_writer_db_url(env)
    builder = build_write_repositories or _build_write_repositories
    return builder(WRITE_BACKEND_SUPABASE, supabase_url=url)
