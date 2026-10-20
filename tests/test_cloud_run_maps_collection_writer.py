"""Mirrors tests/test_cloud_run_maps_import_writer.py's structure for the
Google Maps Collection management screen's own fail-closed connection
routing -- a dedicated role, deliberately separate from the Maps IMPORT
writer role (see maps_collection_writer.py's docstring)."""
import pytest

from src.cloud_run.maps_collection_writer import (
    MAPS_COLLECTION_WRITER_DB_URL_ENV_VAR,
    MapsCollectionWriterNotConfiguredError,
    maps_collection_tasks_repository,
    maps_collection_writer_db_url,
)


def test_missing_env_var_fails_closed_with_no_fallback():
    env = {"SUPABASE_RUNTIME_DB_URL": "postgres://general-write-role/db"}
    with pytest.raises(MapsCollectionWriterNotConfiguredError) as excinfo:
        maps_collection_writer_db_url(env)
    assert MAPS_COLLECTION_WRITER_DB_URL_ENV_VAR in str(excinfo.value)
    assert "SUPABASE_RUNTIME_DB_URL" in str(excinfo.value)


def test_blank_env_var_also_fails_closed():
    env = {MAPS_COLLECTION_WRITER_DB_URL_ENV_VAR: "   "}
    with pytest.raises(MapsCollectionWriterNotConfiguredError):
        maps_collection_writer_db_url(env)


def test_set_env_var_is_used_verbatim():
    env = {MAPS_COLLECTION_WRITER_DB_URL_ENV_VAR: "postgres://maps-collection-writer-role/db"}
    assert maps_collection_writer_db_url(env) == "postgres://maps-collection-writer-role/db"


def test_repository_builder_never_falls_back_to_runtime_url():
    calls = []

    def fake_connect(url):
        calls.append(url)
        return "fake-connection"

    class _FakeRepo:
        def __init__(self, conn):
            self.conn = conn

    env = {
        "SUPABASE_MAPS_COLLECTION_WRITER_DB_URL": "postgres://maps-collection-writer-role/db",
        "SUPABASE_RUNTIME_DB_URL": "postgres://general-write-role/db",
    }
    repo = maps_collection_tasks_repository(env, connect=fake_connect, repository_cls=_FakeRepo)
    assert isinstance(repo, _FakeRepo)
    assert repo.conn == "fake-connection"
    assert calls == ["postgres://maps-collection-writer-role/db"]  # never the runtime URL


def test_repository_builder_raises_before_connecting_when_unconfigured():
    called = []

    def fake_connect(url):
        called.append(url)
        return "should-not-happen"

    with pytest.raises(MapsCollectionWriterNotConfiguredError):
        maps_collection_tasks_repository({}, connect=fake_connect)
    assert called == []
