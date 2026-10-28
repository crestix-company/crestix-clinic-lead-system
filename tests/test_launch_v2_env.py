import os
from pathlib import Path

import pytest

from scripts import launch_v2


def test_normal_mac_email_settings_live_in_user_config_directory():
    assert launch_v2.MAC_EMAIL_ENV_FILE == (
        Path.home() / ".config" / "clinic-lead" / "email_research.env"
    )


@pytest.fixture(autouse=True)
def _clean_runtime_env(monkeypatch):
    """load_runtime_env() also reads CLINIC_WRITE_BACKEND / SUPABASE_RUNTIME_DB_URL from
    the ambient environment before looking at the parsed file; clear them so these tests
    are deterministic regardless of what the outer shell happens to have set."""
    keys = ("SERPAPI_API_KEY", "EMAIL_SEARCH_PROVIDER", "EMAIL_AUTO_RUN_ENABLED",
            "CLINIC_WRITE_BACKEND", "SUPABASE_RUNTIME_DB_URL",
            "CLINIC_DATA_BACKEND", "CLINIC_COMMON_IDENTITY_ENABLED",
            "CLINIC_COMDESK_PREVIEW_ONLY", "CLINIC_READ_ONLY_UI",
            "OUTREACH_GMAIL_DRAFTS_ENABLED", "OUTREACH_GMAIL_OAUTH_TOKEN_JSON",
            "EMAIL_RESEARCH_DB_URL", "EMAIL_TARGET_READER_DB_URL",
            "OUTREACH_GMAIL_OAUTH_CLIENT_JSON", "OUTREACH_GMAIL_EXPECTED_ACCOUNT",
            "SUPABASE_READONLY_DB_URL", "CLINIC_RUNTIME_ENV_FILE", "CLINIC_NORMAL_MAC_RUNTIME")
    original = {key: os.environ.get(key) for key in keys}
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    yield
    # load_runtime_env() intentionally writes to os.environ directly, outside
    # monkeypatch. Restore those values explicitly so a fixture credential from
    # one test can never change backend selection in the rest of the suite.
    for key, value in original.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _write_email_env(root: Path, body: str) -> Path:
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "email_research.env.local"
    path.write_text(body, encoding="utf-8")
    path.chmod(0o600)
    return path


def test_runtime_env_can_be_loaded_from_external_private_writer_file(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    (config / "production_runtime.env").write_text(
        "CLINIC_DATA_BACKEND=supabase\nCLINIC_WRITE_BACKEND=supabase\n", encoding="utf-8"
    )
    external = tmp_path / "private" / "runtime-writer.env"
    external.parent.mkdir()
    external.write_text("SUPABASE_RUNTIME_DB_URL=postgresql://external-writer.invalid/db\n", encoding="utf-8")
    external.chmod(0o600)
    monkeypatch.setenv("CLINIC_RUNTIME_ENV_FILE", str(external))
    local = tmp_path / ".supabase-runtime.env.local"
    local.write_text("SUPABASE_RUNTIME_DB_URL=postgresql://repo-local.invalid/db\n", encoding="utf-8")
    local.chmod(0o600)

    launch_v2.load_runtime_env(tmp_path)

    assert os.environ["SUPABASE_RUNTIME_DB_URL"] == "postgresql://external-writer.invalid/db"


def test_serpapi_api_key_loads_from_local_env(tmp_path):
    _write_email_env(tmp_path, 'SERPAPI_API_KEY=not-a-real-key-fixture-value\n')

    launch_v2.load_runtime_env(tmp_path)

    assert os.environ["SERPAPI_API_KEY"] == "not-a-real-key-fixture-value"


def test_email_search_provider_serpapi_loads_from_local_env(tmp_path):
    _write_email_env(tmp_path, "EMAIL_SEARCH_PROVIDER=serpapi\n")

    launch_v2.load_runtime_env(tmp_path)

    assert os.environ["EMAIL_SEARCH_PROVIDER"] == "serpapi"


def test_both_new_keys_load_together(tmp_path):
    _write_email_env(
        tmp_path,
        "SERPAPI_API_KEY=not-a-real-key-fixture-value\nEMAIL_SEARCH_PROVIDER=serpapi\n",
    )

    launch_v2.load_runtime_env(tmp_path)

    assert os.environ["SERPAPI_API_KEY"] == "not-a-real-key-fixture-value"
    assert os.environ["EMAIL_SEARCH_PROVIDER"] == "serpapi"


def test_unknown_key_is_still_rejected(tmp_path):
    _write_email_env(tmp_path, "SOME_OTHER_SECRET=anything\n")

    with pytest.raises(RuntimeError, match="許可されていない設定"):
        launch_v2.load_runtime_env(tmp_path)


def test_tracked_production_runtime_keys_are_allowlisted(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "production_runtime.env").write_text(
        "CLINIC_DATA_BACKEND=supabase\n"
        "CLINIC_WRITE_BACKEND=supabase\n"
        "CLINIC_COMMON_IDENTITY_ENABLED=0\n",
        encoding="utf-8",
    )
    runtime = tmp_path / ".supabase-runtime.env.local"
    runtime.write_text("SUPABASE_RUNTIME_DB_URL=postgresql://fixture.invalid/db\n", encoding="utf-8")
    runtime.chmod(0o600)

    launch_v2.load_runtime_env(tmp_path)

    assert os.environ["CLINIC_COMMON_IDENTITY_ENABLED"] == "0"


@pytest.mark.parametrize("value", ["0", "1", "true", "false", "yes", "no", "on", "off"])
def test_common_identity_boolean_values_are_accepted(tmp_path, value):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "production_runtime.env").write_text(
        f"CLINIC_COMMON_IDENTITY_ENABLED={value}\n", encoding="utf-8"
    )

    launch_v2.load_runtime_env(tmp_path)

    assert os.environ["CLINIC_COMMON_IDENTITY_ENABLED"] == value


def test_invalid_common_identity_value_fails_closed(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "production_runtime.env").write_text(
        "CLINIC_COMMON_IDENTITY_ENABLED=treu\n", encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="CLINIC_COMMON_IDENTITY_ENABLED"):
        launch_v2.load_runtime_env(tmp_path)


def test_invalid_ambient_common_identity_override_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("CLINIC_COMMON_IDENTITY_ENABLED", "enabled")

    with pytest.raises(RuntimeError, match="CLINIC_COMMON_IDENTITY_ENABLED"):
        launch_v2.load_runtime_env(tmp_path)


def test_api_key_value_never_appears_in_error_message(tmp_path, capsys):
    secret = "sk-fixture-secret-should-never-be-echoed"
    _write_email_env(tmp_path, f"SERPAPI_API_KEY={secret}\nUNKNOWN_KEY=x\n")

    with pytest.raises(RuntimeError) as excinfo:
        launch_v2.load_runtime_env(tmp_path)

    assert secret not in str(excinfo.value)
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err


def test_email_auto_run_enabled_default_false_when_unset(tmp_path):
    _write_email_env(tmp_path, "SERPAPI_API_KEY=not-a-real-key-fixture-value\n")

    launch_v2.load_runtime_env(tmp_path)

    # load_runtime_env must not invent a value for a key absent from the file;
    # the "default false" behavior lives at the consumption site
    # (src/email_research_ui.py: os.environ.get("EMAIL_AUTO_RUN_ENABLED", "false")).
    assert "EMAIL_AUTO_RUN_ENABLED" not in os.environ


def test_preview_only_skips_email_secret_file_and_forces_safe_flags(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "production_runtime.env").write_text(
        "CLINIC_DATA_BACKEND=supabase\nCLINIC_WRITE_BACKEND=supabase\n",
        encoding="utf-8",
    )
    runtime = tmp_path / ".supabase-runtime.env.local"
    runtime.write_text("SUPABASE_RUNTIME_DB_URL=postgresql://fixture.invalid/db\n", encoding="utf-8")
    runtime.chmod(0o600)
    _write_email_env(tmp_path, "UNRELATED_EMAIL_SECRET=never-read\n")
    monkeypatch.setenv("CLINIC_COMDESK_PREVIEW_ONLY", "1")
    monkeypatch.setenv("SUPABASE_READONLY_DB_URL", "postgresql://fixture-reader.invalid/db")
    monkeypatch.delenv("SUPABASE_RUNTIME_DB_URL", raising=False)

    launch_v2.load_runtime_env(tmp_path)

    assert os.environ["CLINIC_READ_ONLY_UI"] == "1"
    assert "SUPABASE_RUNTIME_DB_URL" not in os.environ
    assert os.environ["EMAIL_AUTO_RUN_ENABLED"] == "false"
    assert os.environ["OUTREACH_GMAIL_DRAFTS_ENABLED"] == "false"
    assert "UNRELATED_EMAIL_SECRET" not in os.environ


def test_normal_mac_loads_private_email_repositories_but_not_gmail_or_auto_run(tmp_path, monkeypatch):
    private_email = tmp_path / "runtime" / "email_research.env.local"
    private_email.parent.mkdir()
    private_email.write_text(
        "EMAIL_RESEARCH_DB_URL=postgresql://email-role.invalid/db\n"
        "EMAIL_TARGET_READER_DB_URL=postgresql://reader-role.invalid/db\n"
        "SERPAPI_API_KEY=fixture-secret\n"
        "EMAIL_AUTO_RUN_ENABLED=true\n"
        "OUTREACH_GMAIL_DRAFTS_ENABLED=true\n"
        "OUTREACH_GMAIL_OAUTH_TOKEN_JSON=fixture-oauth\n",
        encoding="utf-8",
    )
    private_email.chmod(0o600)
    monkeypatch.setattr(launch_v2, "MAC_EMAIL_ENV_FILE", private_email)
    monkeypatch.setenv("CLINIC_NORMAL_MAC_RUNTIME", "1")

    launch_v2.load_runtime_env(tmp_path)

    assert os.environ["EMAIL_RESEARCH_DB_URL"] == "postgresql://email-role.invalid/db"
    assert os.environ["EMAIL_TARGET_READER_DB_URL"] == "postgresql://reader-role.invalid/db"
    assert os.environ["SERPAPI_API_KEY"] == "fixture-secret"
    assert os.environ["EMAIL_AUTO_RUN_ENABLED"] == "false"
    assert os.environ["OUTREACH_GMAIL_DRAFTS_ENABLED"] == "false"
    assert "OUTREACH_GMAIL_OAUTH_TOKEN_JSON" not in os.environ


def test_readonly_ui_never_opens_writer_or_email_files(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "production_runtime.env").write_text(
        "CLINIC_DATA_BACKEND=supabase\nCLINIC_WRITE_BACKEND=supabase\n", encoding="utf-8"
    )
    writer = tmp_path / ".supabase-runtime.env.local"
    writer.write_text("NOT_A_RUNTIME_KEY=must-not-be-parsed\n", encoding="utf-8")
    writer.chmod(0o600)
    _write_email_env(tmp_path, "UNRELATED_EMAIL_SECRET=must-not-be-parsed\n")
    monkeypatch.setenv("CLINIC_READ_ONLY_UI", "1")
    monkeypatch.setenv("SUPABASE_READONLY_DB_URL", "postgresql://fixture-reader.invalid/db")

    launch_v2.load_runtime_env(tmp_path)

    assert os.environ["SUPABASE_READONLY_DB_URL"].startswith("postgresql://fixture-reader")
    assert "SUPABASE_RUNTIME_DB_URL" not in os.environ
    assert "UNRELATED_EMAIL_SECRET" not in os.environ


def test_normal_mac_hp_runtime_loads_dedicated_email_settings_without_gmail(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "production_runtime.env").write_text(
        "CLINIC_DATA_BACKEND=supabase\nCLINIC_WRITE_BACKEND=supabase\n", encoding="utf-8"
    )
    writer = tmp_path / "writer.env"
    writer.write_text("SUPABASE_RUNTIME_DB_URL=postgresql://writer.invalid/db\n", encoding="utf-8")
    writer.chmod(0o600)
    monkeypatch.setenv("CLINIC_NORMAL_MAC_RUNTIME", "1")
    monkeypatch.setenv("CLINIC_RUNTIME_ENV_FILE", str(writer))
    monkeypatch.setenv("SUPABASE_READONLY_DB_URL", "postgresql://reader.invalid/db")
    email_file = _write_email_env(
        tmp_path,
        "EMAIL_RESEARCH_DB_URL=postgresql://email.invalid/db\n"
        "SERPAPI_API_KEY=email-search-key\n"
        "OUTREACH_GMAIL_OAUTH_TOKEN_JSON=must-not-load\n"
        "OUTREACH_GMAIL_DRAFTS_ENABLED=true\n",
    )
    monkeypatch.setattr(launch_v2, "MAC_EMAIL_ENV_FILE", email_file)

    launch_v2.load_runtime_env(tmp_path)

    assert os.environ["EMAIL_AUTO_RUN_ENABLED"] == "false"
    assert os.environ["OUTREACH_GMAIL_DRAFTS_ENABLED"] == "false"
    assert os.environ["EMAIL_RESEARCH_DB_URL"] == "postgresql://email.invalid/db"
    assert os.environ["SERPAPI_API_KEY"] == "email-search-key"
    assert "OUTREACH_GMAIL_OAUTH_TOKEN_JSON" not in os.environ


@pytest.mark.parametrize("value", ["true", "yes", "on", "enabled", "2"])
def test_preview_only_flag_rejects_noncanonical_values(tmp_path, monkeypatch, value):
    monkeypatch.setenv("CLINIC_COMDESK_PREVIEW_ONLY", value)
    with pytest.raises(RuntimeError, match="CLINIC_COMDESK_PREVIEW_ONLY"):
        launch_v2.load_runtime_env(tmp_path)


def test_preview_only_requires_supabase_reader_url(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "production_runtime.env").write_text(
        "CLINIC_DATA_BACKEND=sqlite\n", encoding="utf-8"
    )
    monkeypatch.setenv("CLINIC_COMDESK_PREVIEW_ONLY", "1")
    monkeypatch.delenv("SUPABASE_READONLY_DB_URL", raising=False)
    with pytest.raises(RuntimeError, match="専用Reader"):
        launch_v2.load_runtime_env(tmp_path)
