from urllib.parse import unquote, urlsplit

import pytest

from scripts import configure_mac_comdesk_reader as configure


PROJECT = "hwdvyhunozporfuefteb"
RUNTIME = f"postgresql://clinic_runtime.{PROJECT}:runtime-placeholder@aws-0-ap-northeast-1.pooler.supabase.com:5432/postgres"
PASSWORD = "a" * 64


def test_reader_dsn_uses_runtime_endpoint_but_new_role_and_password():
    dsn = configure._reader_dsn(RUNTIME, PASSWORD)
    parsed = urlsplit(dsn)
    assert parsed.hostname == "aws-0-ap-northeast-1.pooler.supabase.com"
    assert unquote(parsed.username) == f"clinic_comdesk_export_reader.{PROJECT}"
    assert unquote(parsed.password) == PASSWORD


def test_configure_writes_private_file_only_after_read_only_preflight(tmp_path, monkeypatch):
    target = tmp_path / "comdesk-reader.env"
    runtime_file = tmp_path / "runtime-writer.env"
    runtime_file.write_text("private fixture only\n", encoding="utf-8")
    runtime_file.chmod(0o600)
    monkeypatch.setattr(configure, "COMDESK_READER_FILE", target)
    monkeypatch.setattr(configure, "RUNTIME_FILE", runtime_file)
    monkeypatch.setattr(configure, "_parse_env_file", lambda *a, **k: {"SUPABASE_RUNTIME_DB_URL": RUNTIME})
    entries = iter((PASSWORD, PASSWORD))
    monkeypatch.setattr(configure.getpass, "getpass", lambda _prompt: next(entries))
    calls = []
    monkeypatch.setattr(configure, "verify_reader", lambda dsn, **kwargs: (calls.append((dsn, kwargs)) or (configure.READER_ROLE, 123)))

    configure.configure()

    assert len(calls) == 1
    assert target.stat().st_mode & 0o777 == 0o600
    assert PASSWORD in target.read_text(encoding="utf-8")


def test_configure_never_overwrites_existing_file(tmp_path, monkeypatch):
    target = tmp_path / "comdesk-reader.env"
    target.write_text("keep existing\n", encoding="utf-8")
    monkeypatch.setattr(configure, "COMDESK_READER_FILE", target)
    monkeypatch.setattr(configure.getpass, "getpass", lambda _prompt: pytest.fail("must not prompt"))

    with pytest.raises(FileExistsError):
        configure.configure()

    assert target.read_text(encoding="utf-8") == "keep existing\n"


def test_configure_does_not_write_when_reader_preflight_fails(tmp_path, monkeypatch):
    target = tmp_path / "comdesk-reader.env"
    runtime_file = tmp_path / "runtime-writer.env"
    runtime_file.write_text("private fixture only\n", encoding="utf-8")
    runtime_file.chmod(0o600)
    monkeypatch.setattr(configure, "COMDESK_READER_FILE", target)
    monkeypatch.setattr(configure, "RUNTIME_FILE", runtime_file)
    monkeypatch.setattr(configure, "_parse_env_file", lambda *a, **k: {"SUPABASE_RUNTIME_DB_URL": RUNTIME})
    entries = iter((PASSWORD, PASSWORD))
    monkeypatch.setattr(configure.getpass, "getpass", lambda _prompt: next(entries))
    monkeypatch.setattr(configure, "verify_reader", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("preflight failed")))

    with pytest.raises(RuntimeError):
        configure.configure()

    assert not target.exists()
