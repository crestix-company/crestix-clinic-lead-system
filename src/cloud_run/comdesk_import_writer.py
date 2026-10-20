"""Dedicated, least-privilege DB connection routing for the Cloud Run Comdesk
import Writer path (src.master.comdesk_cloud_import / app_v2.cloud_comdesk_
import_ui), mirroring src.cloud_run.job_control's proven pattern exactly.

Review finding (Issue #130 PR #131 follow-up): the first version of
cloud_comdesk_import_ui() called write_repositories_for(store), i.e. the
SAME general write connection every other write path (Comdesk/厚生局/Maps
import, refresh_age_model(), etc.) already shares. That gives the Cloud
import panel no narrower a blast radius than the general Writer role, which
defeats the "専用Writer経路で実行し、READ ONLY Webサービスに一般DB書込権限
を与えない" requirement.

Fail-closed, no implicit fallback: SUPABASE_COMDESK_IMPORT_WRITER_DB_URL
must be set explicitly for cloud_comdesk_import_ui() to do anything. This
module never silently reuses SUPABASE_RUNTIME_DB_URL as a substitute. Until
a human provisions a dedicated, least-privilege Postgres role scoped to
Comdesk import writes and sets this variable, the Cloud Comdesk import path
is unusable by design -- same shape as job_control.py's HP Job role
separation. This module itself never creates, grants, or alters any
database role or runs any migration/DDL.
"""
from __future__ import annotations

import os

from src.repository.write_backend import WRITE_BACKEND_SUPABASE
from src.repository.write_backend import build_write_repositories as _build_write_repositories

COMDESK_IMPORT_WRITER_DB_URL_ENV_VAR = "SUPABASE_COMDESK_IMPORT_WRITER_DB_URL"
RUNTIME_DB_URL_ENV_VAR = "SUPABASE_RUNTIME_DB_URL"


class ComdeskImportWriterNotConfiguredError(RuntimeError):
    pass


def comdesk_import_writer_db_url(env=None):
    env = os.environ if env is None else env
    url = (env.get(COMDESK_IMPORT_WRITER_DB_URL_ENV_VAR) or "").strip()
    if not url:
        raise ComdeskImportWriterNotConfiguredError(
            f"{COMDESK_IMPORT_WRITER_DB_URL_ENV_VAR}が未設定です。Cloud Run Comdesk取込では"
            f"一般DB接続（{RUNTIME_DB_URL_ENV_VAR}）への暗黙フォールバックを行いません。"
            "Comdesk取込専用の最小権限DBロール・接続URLを人間が明示的に設定してください。"
        )
    return url


def comdesk_import_writer_repositories(env=None, *, build_write_repositories=None):
    """Return a WriteRepositories bundle connected with the dedicated Comdesk
    import writer URL. Callers must not reuse this connection for unrelated
    writes -- those stay on the general write backend via write_repositories_for()."""
    url = comdesk_import_writer_db_url(env)
    builder = build_write_repositories or _build_write_repositories
    return builder(WRITE_BACKEND_SUPABASE, supabase_url=url)
