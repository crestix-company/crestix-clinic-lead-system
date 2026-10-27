"""Regression tests for safe Supabase/Email connection setup and recovery."""
from types import SimpleNamespace

import psycopg
import pytest


def test_runtime_env_file_wins_over_blank_export_without_printing_secret(tmp_path, monkeypatch):
    import os
    from scripts.launch_v2 import load_runtime_env
    keys = ("CLINIC_DATA_BACKEND", "CLINIC_WRITE_BACKEND", "SUPABASE_RUNTIME_DB_URL")
    original = {key: os.environ.get(key) for key in keys}

    (tmp_path / "config").mkdir()
    (tmp_path / "config/production_runtime.env").write_text(
        "CLINIC_DATA_BACKEND=supabase\nCLINIC_WRITE_BACKEND=supabase\n", encoding="utf-8")
    runtime_file = tmp_path / ".supabase-runtime.env.local"
    runtime_file.write_text("SUPABASE_RUNTIME_DB_URL=postgresql://safe-secret@private-host/db\n", encoding="utf-8")
    runtime_file.chmod(0o600)
    monkeypatch.setenv("SUPABASE_RUNTIME_DB_URL", "")
    monkeypatch.delenv("CLINIC_DATA_BACKEND", raising=False)
    monkeypatch.delenv("CLINIC_WRITE_BACKEND", raising=False)

    try:
        assert load_runtime_env(tmp_path) is True
        assert os.environ["CLINIC_DATA_BACKEND"] == "supabase"
        assert os.environ["CLINIC_WRITE_BACKEND"] == "supabase"
        assert os.environ["SUPABASE_RUNTIME_DB_URL"].endswith("/db")
    finally:
        # load_runtime_env intentionally writes process env directly. Restore the exact
        # incoming state so simulated routing cannot leak into later tests.
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.mark.parametrize(("exc", "category", "retry"), [
    (psycopg.OperationalError("password authentication failed for secret user"), "authentication", False),
    (psycopg.OperationalError("connection timeout expired"), "timeout", True),
    (psycopg.InterfaceError("connection already closed"), "disconnected", True),
    (psycopg.OperationalError("canceling statement due to statement timeout"), "query_timeout", False),
])
def test_connection_error_categories_are_safe_and_retry_policy_is_read_only(exc, category, retry):
    from src.repository.connection_diagnostics import (
        connection_error_category, retryable_read_connection_error, safe_connection_message,
    )

    assert connection_error_category(exc) == category
    assert retryable_read_connection_error(exc) is retry
    message = safe_connection_message(exc)
    assert "secret" not in message and "password" not in message and "private-host" not in message


def test_permission_denial_is_not_misreported_as_a_connection_failure():
    from src.repository.connection_diagnostics import (
        connection_error_category, is_database_connection_exception, safe_connection_message,
    )

    class PermissionDenied(Exception):
        sqlstate = "42501"

    exc = PermissionDenied("permission denied for table settings")
    assert connection_error_category(exc) == "permission"
    assert is_database_connection_exception(exc) is True
    assert "接続しましたが" in safe_connection_message(exc)
    assert "settings" not in safe_connection_message(exc)


def test_runtime_store_cache_key_changes_when_runtime_credentials_change(monkeypatch):
    import app_v2

    monkeypatch.setenv("CLINIC_DATA_BACKEND", "supabase")
    monkeypatch.setenv("CLINIC_WRITE_BACKEND", "supabase")
    monkeypatch.setenv("SUPABASE_RUNTIME_DB_URL", "postgresql://first-secret@first-host/db")
    first_key = app_v2.runtime_store_cache_key()
    monkeypatch.setenv("SUPABASE_RUNTIME_DB_URL", "postgresql://second-secret@second-host/db")
    second_key = app_v2.runtime_store_cache_key()
    assert first_key != second_key
    assert "first-secret" not in first_key and "first-host" not in first_key


def test_direct_streamlit_launch_with_missing_runtime_config_stops_without_fallback(monkeypatch):
    from streamlit.testing.v1 import AppTest
    import app_v2

    monkeypatch.setenv("CLINIC_DATA_BACKEND", "supabase")
    monkeypatch.setenv("CLINIC_WRITE_BACKEND", "supabase")
    monkeypatch.delenv("SUPABASE_RUNTIME_DB_URL", raising=False)
    app_v2.store_for.clear()
    monkeypatch.setattr("psycopg.connect", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("configuration gate must stop before any DB connection")))
    app = AppTest.from_file(str(app_v2.ROOT / "app_v2.py"), default_timeout=30).run()
    assert not app.exception
    messages = [element.value for element in app.error]
    assert any("接続設定がありません" in message for message in messages)
    assert any("正式ランチャー" in element.value for element in app.caption)
    app_v2.store_for.clear()


def test_streamlit_resource_cache_does_not_reuse_store_after_config_change(monkeypatch):
    import app_v2

    made = []

    def factory():
        value = object()
        made.append(value)
        return value

    monkeypatch.setenv("CLINIC_DATA_BACKEND", "supabase")
    monkeypatch.setattr("src.repository.runtime_store.SupabaseRuntimeStore", factory)
    app_v2.store_for.clear()
    try:
        monkeypatch.setenv("SUPABASE_RUNTIME_DB_URL", "postgresql://one@host/db")
        first = app_v2.store_for(None, app_v2.runtime_store_cache_key())
        assert app_v2.store_for(None, app_v2.runtime_store_cache_key()) is first
        monkeypatch.setenv("SUPABASE_RUNTIME_DB_URL", "postgresql://two@host/db")
        second = app_v2.store_for(None, app_v2.runtime_store_cache_key())
        assert second is not first
        assert len(made) == 2
    finally:
        app_v2.store_for.clear()


def test_cached_runtime_store_reconnects_after_query_detects_disconnect(monkeypatch):
    import app_v2
    from src.repository.runtime_store import SupabaseRuntimeStore

    class Conn:
        closed = False
        broken = False

    first_conn, fresh_conn = Conn(), Conn()
    first_repo = SimpleNamespace(_conn=first_conn,
        metrics=lambda: (_ for _ in ()).throw(psycopg.OperationalError("server closed the connection")))
    fresh_repo = SimpleNamespace(_conn=fresh_conn, metrics=lambda: {"count": 3})
    bundles = iter([SimpleNamespace(clinics=first_repo), SimpleNamespace(clinics=fresh_repo)])
    monkeypatch.setattr("src.repository.backend.build_repositories", lambda *_a, **_k: next(bundles))
    monkeypatch.setenv("CLINIC_DATA_BACKEND", "supabase")
    monkeypatch.setenv("SUPABASE_RUNTIME_DB_URL", "postgresql://fake@host/db")
    app_v2.store_for.clear()
    try:
        store = app_v2.store_for(None, app_v2.runtime_store_cache_key())
        assert isinstance(store, SupabaseRuntimeStore)
        assert app_v2.store_for(None, app_v2.runtime_store_cache_key()) is store
        assert store.metrics() == {"count": 3}
        assert store._conn is fresh_conn
    finally:
        app_v2.store_for.clear()


def test_email_ui_diagnostic_retains_category_without_secrets(monkeypatch):
    from src.repository.email_database import email_database_ui_status, note_email_database_connection_error

    monkeypatch.setenv("EMAIL_RESEARCH_DB_URL", "postgresql://email:secret@email.example.test/email")
    monkeypatch.setenv("SUPABASE_RUNTIME_DB_URL", "postgresql://clinic:secret@clinic.example.test/clinic")
    note_email_database_connection_error(psycopg.OperationalError("password authentication failed for secret"))
    status = email_database_ui_status()
    assert status.ready is False
    assert "認証" in status.message
    assert "secret" not in status.message and "example.test" not in status.message
