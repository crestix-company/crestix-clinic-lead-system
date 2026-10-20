"""Mirrors tests/test_cloud_run_comdesk_import_writer.py exactly."""
import pytest

from src.cloud_run.kouseikyoku_import_writer import (
    KOUSEIKYOKU_IMPORT_WRITER_DB_URL_ENV_VAR,
    KouseikyokuImportWriterNotConfiguredError,
    kouseikyoku_import_writer_db_url,
    kouseikyoku_import_writer_repositories,
)


def test_missing_env_var_fails_closed_with_no_fallback():
    env = {"SUPABASE_RUNTIME_DB_URL": "postgres://general-write-role/db"}
    with pytest.raises(KouseikyokuImportWriterNotConfiguredError) as excinfo:
        kouseikyoku_import_writer_db_url(env)
    assert KOUSEIKYOKU_IMPORT_WRITER_DB_URL_ENV_VAR in str(excinfo.value)
    assert "SUPABASE_RUNTIME_DB_URL" in str(excinfo.value)


def test_blank_env_var_also_fails_closed():
    env = {KOUSEIKYOKU_IMPORT_WRITER_DB_URL_ENV_VAR: "  "}
    with pytest.raises(KouseikyokuImportWriterNotConfiguredError):
        kouseikyoku_import_writer_db_url(env)


def test_set_env_var_is_used_verbatim():
    env = {KOUSEIKYOKU_IMPORT_WRITER_DB_URL_ENV_VAR: "postgres://kouseikyoku-import-writer-role/db"}
    assert kouseikyoku_import_writer_db_url(env) == "postgres://kouseikyoku-import-writer-role/db"


def test_repositories_builder_never_falls_back_to_runtime_url():
    calls = []

    def fake_builder(backend, *, supabase_url):
        calls.append((backend, supabase_url))
        return "fake-repositories"

    env = {
        KOUSEIKYOKU_IMPORT_WRITER_DB_URL_ENV_VAR: "postgres://kouseikyoku-import-writer-role/db",
        "SUPABASE_RUNTIME_DB_URL": "postgres://general-write-role/db",
    }
    result = kouseikyoku_import_writer_repositories(env, build_write_repositories=fake_builder)
    assert result == "fake-repositories"
    assert len(calls) == 1
    assert calls[0][1] == "postgres://kouseikyoku-import-writer-role/db"


def test_repositories_builder_raises_before_calling_builder_when_unconfigured():
    called = []

    def fake_builder(*_args, **_kwargs):
        called.append(True)
        return "should-not-happen"

    with pytest.raises(KouseikyokuImportWriterNotConfiguredError):
        kouseikyoku_import_writer_repositories({}, build_write_repositories=fake_builder)
    assert called == []
