import os
from pathlib import Path

import pytest

from scripts import launch_v2


@pytest.fixture(autouse=True)
def _clean_runtime_env(monkeypatch):
    """load_runtime_env() also reads CLINIC_WRITE_BACKEND / SUPABASE_RUNTIME_DB_URL from
    the ambient environment before looking at the parsed file; clear them so these tests
    are deterministic regardless of what the outer shell happens to have set."""
    for key in ("SERPAPI_API_KEY", "EMAIL_SEARCH_PROVIDER", "EMAIL_AUTO_RUN_ENABLED",
                "CLINIC_WRITE_BACKEND", "SUPABASE_RUNTIME_DB_URL",
                "CLINIC_COMMON_IDENTITY_ENABLED"):
        monkeypatch.delenv(key, raising=False)


def _write_email_env(root: Path, body: str) -> Path:
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "email_research.env.local"
    path.write_text(body, encoding="utf-8")
    path.chmod(0o600)
    return path


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
