"""Mirrors tests/test_cloud_run_job_control.py's structure for the Comdesk
import writer's own fail-closed connection routing."""
import pytest

from src.cloud_run.comdesk_import_writer import (
    COMDESK_IMPORT_WRITER_DB_URL_ENV_VAR,
    ComdeskImportWriterNotConfiguredError,
    comdesk_import_writer_db_url,
    comdesk_import_writer_repositories,
)


def test_missing_env_var_fails_closed_with_no_fallback():
    env = {"SUPABASE_RUNTIME_DB_URL": "postgres://general-write-role/db"}
    with pytest.raises(ComdeskImportWriterNotConfiguredError) as excinfo:
        comdesk_import_writer_db_url(env)
    assert COMDESK_IMPORT_WRITER_DB_URL_ENV_VAR in str(excinfo.value)
    assert "SUPABASE_RUNTIME_DB_URL" in str(excinfo.value)  # explains it won't fall back to this


def test_blank_env_var_also_fails_closed():
    env = {COMDESK_IMPORT_WRITER_DB_URL_ENV_VAR: "   "}
    with pytest.raises(ComdeskImportWriterNotConfiguredError):
        comdesk_import_writer_db_url(env)


def test_set_env_var_is_used_verbatim():
    env = {COMDESK_IMPORT_WRITER_DB_URL_ENV_VAR: "postgres://comdesk-import-writer-role/db"}
    assert comdesk_import_writer_db_url(env) == "postgres://comdesk-import-writer-role/db"


def test_repositories_builder_never_falls_back_to_runtime_url():
    calls = []

    def fake_builder(backend, *, supabase_url):
        calls.append((backend, supabase_url))
        return "fake-repositories"

    env = {
        COMDESK_IMPORT_WRITER_DB_URL_ENV_VAR: "postgres://comdesk-import-writer-role/db",
        "SUPABASE_RUNTIME_DB_URL": "postgres://general-write-role/db",
    }
    result = comdesk_import_writer_repositories(env, build_write_repositories=fake_builder)
    assert result == "fake-repositories"
    assert len(calls) == 1
    assert calls[0][1] == "postgres://comdesk-import-writer-role/db"  # never the runtime URL


def test_repositories_builder_raises_before_calling_builder_when_unconfigured():
    called = []

    def fake_builder(*_args, **_kwargs):
        called.append(True)
        return "should-not-happen"

    with pytest.raises(ComdeskImportWriterNotConfiguredError):
        comdesk_import_writer_repositories({}, build_write_repositories=fake_builder)
    assert called == []
