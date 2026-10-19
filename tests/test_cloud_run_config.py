"""Phase A: Cloud Run Web (clinic-lead-web) 起動・設定・READ ONLY検証のテスト。

実際のDocker buildやGCPへの接続は行わない。Dockerfile.cloudrun/.dockerignoreの
テキスト内容と、scripts/verify_cloud_runtime.pyのロジックのみを検証する。
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _read(name):
    return (ROOT / name).read_text(encoding="utf-8")


def test_dockerfile_cloudrun_uses_python312_slim():
    text = _read("Dockerfile.cloudrun")
    assert "FROM python:3.12-slim" in text


def test_dockerfile_cloudrun_listens_on_injected_port_and_all_interfaces():
    text = _read("Dockerfile.cloudrun")
    assert "--server.address=0.0.0.0" in text
    assert "--server.port=$PORT" in text
    assert "streamlit run app_v2.py" in text


def test_dockerfile_cloudrun_runs_as_non_root():
    text = _read("Dockerfile.cloudrun")
    assert "USER clinicapp" in text
    assert "useradd" in text


def test_dockerfile_cloudrun_uses_requirements_txt_not_lock():
    text = _read("Dockerfile.cloudrun")
    assert "requirements.txt" in text
    assert "requirements-lock.txt" not in text


def test_dockerfile_cloudrun_never_copies_data_or_secret_paths():
    text = _read("Dockerfile.cloudrun")
    for forbidden in ("COPY data", "COPY .env", "COPY .supabase-runtime.env.local", "COPY code_backup"):
        assert forbidden not in text


def test_dockerignore_defaults_to_excluding_everything():
    text = _read(".dockerignore")
    lines = [line.strip() for line in text.splitlines()]
    assert "*" in lines


def test_dockerignore_excludes_real_clinic_data_and_credentials():
    text = _read(".dockerignore")
    for pattern in (
        "*.sqlite3*",
        "*.csv",
        ".env",
        ".supabase-runtime.env.local",
        "config/email_research.env.local",
        "config/gmail_oauth_client.json",
        "config/gmail_oauth_token.json",
    ):
        assert pattern in text, f"{pattern!r} should be denied in .dockerignore"


def test_dockerignore_allowlists_only_the_app_runtime_paths():
    text = _read(".dockerignore")
    allowed = {line.lstrip("!").strip() for line in text.splitlines() if line.startswith("!")}
    assert allowed == {"app_v2.py", "requirements.txt", "src", "src/**", "config", "config/**"}


class _FakeCursor:
    def __init__(self, show_value, select_value=1):
        self._show_value = show_value
        self._select_value = select_value
        self._last = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, *args):
        self._last = sql

    def fetchone(self):
        if self._last == "SHOW default_transaction_read_only":
            return (self._show_value,)
        return (self._select_value,)


class _FakeConnection:
    def __init__(self, show_value, select_value=1):
        self._cursor = _FakeCursor(show_value, select_value)
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


def test_check_environment_all_pass_when_properly_configured(monkeypatch):
    from scripts.verify_cloud_runtime import check_environment

    env = {
        "CLINIC_READ_ONLY_UI": "true",
        "OUTREACH_GMAIL_DRAFTS_ENABLED": "false",
        "EMAIL_AUTO_RUN_ENABLED": "",
        "CLINIC_DATA_BACKEND": "supabase",
        "CLINIC_WRITE_BACKEND": "supabase",
        "SUPABASE_RUNTIME_DB_URL": "postgresql://example.invalid/postgres",
        "PORT": "8080",
    }
    results = check_environment(env)
    assert all(results.values()), results


@pytest.mark.parametrize(
    "mutation",
    [
        {"CLINIC_READ_ONLY_UI": "false"},
        {"OUTREACH_GMAIL_DRAFTS_ENABLED": "true"},
        {"EMAIL_AUTO_RUN_ENABLED": "true"},
        {"CLINIC_DATA_BACKEND": "sqlite"},
        {"SUPABASE_RUNTIME_DB_URL": ""},
        {"PORT": ""},
    ],
)
def test_check_environment_fails_closed_on_unsafe_config(mutation):
    from scripts.verify_cloud_runtime import check_environment

    env = {
        "CLINIC_READ_ONLY_UI": "true",
        "OUTREACH_GMAIL_DRAFTS_ENABLED": "false",
        "EMAIL_AUTO_RUN_ENABLED": "",
        "CLINIC_DATA_BACKEND": "supabase",
        "CLINIC_WRITE_BACKEND": "supabase",
        "SUPABASE_RUNTIME_DB_URL": "postgresql://example.invalid/postgres",
        "PORT": "8080",
    }
    env.update(mutation)
    results = check_environment(env)
    assert not all(results.values())


def test_check_read_only_db_session_skips_without_url():
    from scripts.verify_cloud_runtime import check_read_only_db_session

    report = check_read_only_db_session("")
    assert report == {"skipped": True, "reason": "SUPABASE_RUNTIME_DB_URL is not set"}


def test_check_read_only_db_session_passes_when_guard_is_on():
    from scripts.verify_cloud_runtime import check_read_only_db_session

    fake_conn = _FakeConnection(show_value="on")
    report = check_read_only_db_session(
        "postgresql://example.invalid/postgres", connect=lambda url: fake_conn
    )
    assert report == {"skipped": False, "read_only_guard_on": True, "select_ok": True}
    assert fake_conn.closed is True


def test_check_read_only_db_session_fails_when_guard_is_off():
    from scripts.verify_cloud_runtime import check_read_only_db_session

    fake_conn = _FakeConnection(show_value="off")
    report = check_read_only_db_session(
        "postgresql://example.invalid/postgres", connect=lambda url: fake_conn
    )
    assert report["read_only_guard_on"] is False


def test_check_read_only_db_session_never_executes_writes():
    from scripts.verify_cloud_runtime import check_read_only_db_session

    executed = []

    class RecordingCursor(_FakeCursor):
        def execute(self, sql, *args):
            executed.append(sql)
            super().execute(sql, *args)

    class RecordingConnection(_FakeConnection):
        def __init__(self):
            super().__init__(show_value="on")
            self._cursor = RecordingCursor(show_value="on")

    check_read_only_db_session(
        "postgresql://example.invalid/postgres", connect=lambda url: RecordingConnection()
    )
    for statement in executed:
        upper = statement.upper()
        assert not any(verb in upper for verb in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE"))


def test_run_reports_ok_true_for_fully_safe_configuration():
    from scripts.verify_cloud_runtime import run

    env = {
        "CLINIC_READ_ONLY_UI": "true",
        "OUTREACH_GMAIL_DRAFTS_ENABLED": "false",
        "EMAIL_AUTO_RUN_ENABLED": "",
        "CLINIC_DATA_BACKEND": "supabase",
        "CLINIC_WRITE_BACKEND": "supabase",
        "SUPABASE_RUNTIME_DB_URL": "postgresql://example.invalid/postgres",
        "PORT": "8080",
    }
    fake_conn = _FakeConnection(show_value="on")
    report = run(env, connect=lambda url: fake_conn)
    assert report["ok"] is True


def test_run_reports_ok_false_when_read_only_ui_disabled():
    from scripts.verify_cloud_runtime import run

    env = {
        "CLINIC_READ_ONLY_UI": "false",
        "OUTREACH_GMAIL_DRAFTS_ENABLED": "false",
        "EMAIL_AUTO_RUN_ENABLED": "",
        "CLINIC_DATA_BACKEND": "supabase",
        "CLINIC_WRITE_BACKEND": "supabase",
        "SUPABASE_RUNTIME_DB_URL": "postgresql://example.invalid/postgres",
        "PORT": "8080",
    }
    report = run(env, skip_db_check=True)
    assert report["ok"] is False


def test_run_never_includes_the_db_url_value_in_the_report():
    from scripts.verify_cloud_runtime import run

    secret_url = "postgresql://user:sekret-password@example.invalid/postgres"
    env = {
        "CLINIC_READ_ONLY_UI": "true",
        "OUTREACH_GMAIL_DRAFTS_ENABLED": "false",
        "EMAIL_AUTO_RUN_ENABLED": "",
        "CLINIC_DATA_BACKEND": "supabase",
        "CLINIC_WRITE_BACKEND": "supabase",
        "SUPABASE_RUNTIME_DB_URL": secret_url,
        "PORT": "8080",
    }
    fake_conn = _FakeConnection(show_value="on")
    report = run(env, connect=lambda url: fake_conn)
    assert "sekret-password" not in str(report)


def test_existing_mac_launcher_is_untouched_by_cloud_run_work():
    text = _read("scripts/launch_v2_mac.sh")
    assert "8502" in text
    assert "launch_v2.py" in text
