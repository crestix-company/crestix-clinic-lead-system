from pathlib import Path
from types import SimpleNamespace

import pytest

from src.repository import mac_feature_access as access


PROJECT = "aaaaaaaaaaaaaaaaaaaa"
RUNTIME = f"postgresql://clinic_runtime:fixture@db.{PROJECT}.supabase.co/postgres"


@pytest.fixture
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(access, "WRITERS_FILE", tmp_path / "feature-writers.env")
    monkeypatch.setattr(access, "COMDESK_READER_FILE", tmp_path / "comdesk-reader.env")
    monkeypatch.setenv("SUPABASE_RUNTIME_DB_URL", RUNTIME)
    return tmp_path


def _write(path: Path, value: str, mode=0o600):
    path.write_text(value, encoding="utf-8")
    path.chmod(mode)


def test_feature_writer_fails_closed_without_private_scoped_dsn(files, monkeypatch):
    from src.repository import write_backend

    calls = []
    monkeypatch.setattr(write_backend, "build_write_repositories", lambda *a, **k: calls.append((a, k)))
    assert access.feature_writer_configured("comdesk_import") is False
    with pytest.raises(access.FeatureConnectionUnavailable):
        access.feature_writer_repositories("comdesk_import")
    assert calls == []


def test_writer_rejects_runtime_role_and_public_file(files):
    _write(access.WRITERS_FILE, f"SUPABASE_COMDESK_IMPORT_WRITER_DB_URL={RUNTIME}\n")
    assert access.feature_writer_configured("comdesk_import") is False
    _write(
        access.WRITERS_FILE,
        f"SUPABASE_COMDESK_IMPORT_WRITER_DB_URL=postgresql://clinic_comdesk_import_writer:fixture@db.{PROJECT}.supabase.co/postgres\n",
        mode=0o644,
    )
    assert access.feature_writer_configured("comdesk_import") is False
    with pytest.raises(access.FeatureConnectionUnavailable, match="mode 600"):
        access.feature_writer_repositories("comdesk_import")


def test_writer_checks_actual_database_role_and_rolls_back_probe(files, monkeypatch):
    from src.repository import write_backend

    dsn = f"postgresql://clinic_maps_import_writer:fixture@db.{PROJECT}.supabase.co/postgres"
    _write(access.WRITERS_FILE, f"SUPABASE_MAPS_IMPORT_WRITER_DB_URL={dsn}\n")
    assert access.feature_writer_configured("maps_import") is True

    class Connection:
        closed = False
        rolled_back = False
        actual = "clinic_maps_import_writer"

        def cursor(self):
            parent = self

            class Cursor:
                def __enter__(self): return self
                def __exit__(self, *args): return False
                def execute(self, sql): assert sql == "SELECT current_user"
                def fetchone(self): return (parent.actual,)

            return Cursor()

        def rollback(self): self.rolled_back = True
        def close(self): self.closed = True

    conn = Connection()
    bundle = SimpleNamespace(clinics=SimpleNamespace(_conn=conn))
    monkeypatch.setattr(write_backend, "build_write_repositories", lambda backend, *, supabase_url: bundle)
    assert access.feature_writer_repositories("maps_import") is bundle
    assert conn.rolled_back is True
    conn.actual = "clinic_runtime"
    with pytest.raises(access.FeatureConnectionUnavailable, match="DBロール"):
        access.feature_writer_repositories("maps_import")
    assert conn.closed is True


def test_comdesk_reader_requires_distinct_role_and_read_only_preflight(files, monkeypatch):
    from scripts import launch_v2_mac_reader
    from src.repository import backend

    dsn = f"postgresql://clinic_comdesk_export_reader:fixture@db.{PROJECT}.supabase.co/postgres"
    _write(access.COMDESK_READER_FILE, f"{access.READER_KEY}={dsn}\n")
    assert access.comdesk_reader_configured() is True
    verified = []
    monkeypatch.setattr(
        launch_v2_mac_reader, "verify_reader",
        lambda url, **kwargs: (verified.append((url, kwargs)) or (access.READER_ROLE, 100)),
    )
    conn = SimpleNamespace()
    bundle = SimpleNamespace(clinics=SimpleNamespace(_conn=conn))
    monkeypatch.setattr(backend, "build_repositories", lambda name, *, supabase_url: bundle)

    store = access.comdesk_reader_store()

    assert store._conn is conn
    assert verified[0][0] == dsn
    assert verified[0][1]["required_columns"] == access.COMDESK_READER_COLUMNS
    assert "public.clinics" in verified[0][1]["required_tables"]
    assert "email_research.email_candidates" not in verified[0][1]["required_tables"]
