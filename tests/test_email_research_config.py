import pytest
import os
from src.master.email_research_config import load_email_research_config


def test_performance_defaults_are_configured():
    cfg = load_email_research_config({})
    assert (cfg.workers, cfg.read_timeout_seconds, cfg.retry_count, cfg.search_query_limit) == (8, 20, 1, 2)


def test_query_limit_cannot_be_raised_above_two():
    with pytest.raises(ValueError):
        load_email_research_config({"EMAIL_RESEARCH_SEARCH_QUERY_LIMIT": "3"})


def test_idle_poll_is_configurable_and_positive():
    assert load_email_research_config({"EMAIL_RESEARCH_IDLE_POLL_SECONDS": "0.25"}).idle_poll_seconds == 0.25
    with pytest.raises(ValueError):
        load_email_research_config({"EMAIL_RESEARCH_IDLE_POLL_SECONDS": "0"})


def test_private_email_config_accepts_autorun_disabled(tmp_path, monkeypatch):
    from scripts.launch_v2 import load_runtime_env

    monkeypatch.setenv("EMAIL_AUTO_RUN_ENABLED", "true")
    monkeypatch.delenv("SUPABASE_RUNTIME_DB_URL", raising=False)
    monkeypatch.delenv("CLINIC_WRITE_BACKEND", raising=False)
    email_config = tmp_path / "config" / "email_research.env.local"
    email_config.parent.mkdir()
    email_config.write_text(
        "EMAIL_AUTO_RUN_ENABLED=false\n"
        "EMAIL_RESEARCH_DB_URL=postgresql://email@example.invalid/email\n"
        "EMAIL_TARGET_READER_DB_URL=postgresql://clinic_email_target_reader@example.invalid/postgres\n"
        "CLINIC_READONLY_DB_URL=postgresql://reader@example.invalid/clinic\n",
        encoding="utf-8",
    )
    email_config.chmod(0o600)

    load_runtime_env(tmp_path)

    assert os.environ["EMAIL_AUTO_RUN_ENABLED"] == "false"
    assert os.environ["EMAIL_TARGET_READER_DB_URL"].startswith("postgresql://clinic_email_target_reader")


def test_autorun_defaults_disabled_when_unset(monkeypatch):
    monkeypatch.delenv("EMAIL_AUTO_RUN_ENABLED", raising=False)
    assert os.environ.get("EMAIL_AUTO_RUN_ENABLED", "false") == "false"


def test_private_email_config_rejects_invalid_autorun_value(tmp_path):
    from scripts.launch_v2 import load_runtime_env

    email_config = tmp_path / "config" / "email_research.env.local"
    email_config.parent.mkdir()
    email_config.write_text("EMAIL_AUTO_RUN_ENABLED=on\n", encoding="utf-8")
    email_config.chmod(0o600)

    with pytest.raises(RuntimeError, match="EMAIL_AUTO_RUN_ENABLED"):
        load_runtime_env(tmp_path)
