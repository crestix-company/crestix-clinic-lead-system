"""Stage4-D Gate2-A offline tests. No live Supabase connection is opened by this file --
the Supabase adapter is exercised against a fake psycopg-shaped connection/cursor that records
executed SQL and simulates RETURNING values, per the Owner's explicit allowance ("DB actual
generationはmock/test contractで検証") since this pass performs no Live DDL/DML.
"""
import json
import os
import sqlite3
import tempfile

import pytest

from src.master.identity_contract import (
    IdentityContractError, resolve_medical_key_transition, validate_existing_medical_key,
    generate_medical_key_for_new_clinic, refresh_clinic_projection_preserving_identity,
)
from src.repository.high_range_id import assert_high_range, HighRangeIdViolation, RESERVED_ID_FLOOR
from src.repository.write_backend import active_write_backend, WRITE_BACKEND_ENV_VAR


# ---------------------------------------------------------------------------------------------
# Owner Decision 1: medical_key transition table
# ---------------------------------------------------------------------------------------------

def test_existing_nonblank_same_candidate_is_noop():
    assert resolve_medical_key_transition("東京都:医科:123", "東京都:医科:123", authoritative=False) == "東京都:医科:123"


def test_existing_nonblank_different_candidate_rejected():
    with pytest.raises(IdentityContractError):
        resolve_medical_key_transition("東京都:医科:123", "東京都:医科:999", authoritative=False)


def test_existing_nonblank_to_blank_rejected():
    with pytest.raises(IdentityContractError):
        resolve_medical_key_transition("東京都:医科:123", "", authoritative=False)


def test_existing_nonblank_to_blank_rejected_even_if_authoritative():
    # known -> blank is forbidden unconditionally; authoritative only ever unlocks blank -> known.
    with pytest.raises(IdentityContractError):
        resolve_medical_key_transition("東京都:医科:123", "", authoritative=True)


def test_existing_blank_to_known_requires_authoritative():
    with pytest.raises(IdentityContractError):
        resolve_medical_key_transition("", "東京都:医科:123", authoritative=False)


def test_existing_blank_to_known_authoritative_allowed_once():
    assert resolve_medical_key_transition("", "東京都:医科:123", authoritative=True) == "東京都:医科:123"


def test_existing_blank_stays_blank_noop():
    assert resolve_medical_key_transition("", "", authoritative=False) == ""


def test_validate_existing_medical_key_never_allows_completion():
    assert validate_existing_medical_key("", "") == ""
    with pytest.raises(IdentityContractError):
        validate_existing_medical_key("", "東京都:医科:123")


def test_generate_medical_key_for_new_clinic_requires_authoritative_source():
    record = {"clinic_id": "東京都:1234", "prefecture": "東京都", "medical_type": "医科"}
    assert generate_medical_key_for_new_clinic(record, is_authoritative_official_source=False) == ""
    assert generate_medical_key_for_new_clinic(record, is_authoritative_official_source=True) == "東京都:医科:1234"


# ---------------------------------------------------------------------------------------------
# Owner Decision 2: high-range numeric ID guard
# ---------------------------------------------------------------------------------------------

def test_high_range_guard_accepts_values_at_or_above_floor():
    assert assert_high_range(RESERVED_ID_FLOOR, table="public.clinics") == RESERVED_ID_FLOOR
    assert assert_high_range(RESERVED_ID_FLOOR + 1, table="provenance.source_records") == RESERVED_ID_FLOOR + 1


def test_high_range_guard_rejects_values_below_floor():
    with pytest.raises(HighRangeIdViolation):
        assert_high_range(999_999_999, table="public.clinics")


def test_high_range_guard_rejects_unscoped_tables():
    with pytest.raises(AssertionError):
        assert_high_range(RESERVED_ID_FLOOR, table="provenance.change_history")


# ---------------------------------------------------------------------------------------------
# WRITE backend selector
# ---------------------------------------------------------------------------------------------

def test_write_backend_defaults_to_sqlite(monkeypatch):
    monkeypatch.delenv(WRITE_BACKEND_ENV_VAR, raising=False)
    assert active_write_backend() == "sqlite"


def test_write_backend_rejects_invalid_value(monkeypatch):
    monkeypatch.setenv(WRITE_BACKEND_ENV_VAR, "mysql")
    with pytest.raises(ValueError):
        active_write_backend()


def test_write_backend_accepts_supabase(monkeypatch):
    monkeypatch.setenv(WRITE_BACKEND_ENV_VAR, "supabase")
    assert active_write_backend() == "supabase"


def test_supabase_write_factory_requires_runtime_url_without_admin_fallback(monkeypatch):
    from src.repository.write_backend import build_write_repositories
    monkeypatch.delenv("SUPABASE_RUNTIME_DB_URL", raising=False)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://admin-credential-must-not-be-used")
    with pytest.raises(RuntimeError, match="SUPABASE_RUNTIME_DB_URL"):
        build_write_repositories("supabase")


def test_supabase_write_factory_uses_runtime_url(monkeypatch):
    from src.repository.write_backend import build_write_repositories
    import src.repository.supabase_adapter as adapter
    conn = FakeConn()
    seen = []
    monkeypatch.setenv("SUPABASE_RUNTIME_DB_URL", "postgresql://runtime-only")
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://admin-must-not-win")
    monkeypatch.setattr(adapter, "connect", lambda url, autocommit=False: seen.append((url, autocommit)) or conn)
    repositories = build_write_repositories("supabase")
    assert repositories.clinics._conn is conn
    assert seen == [("postgresql://runtime-only", False)]


def test_production_launcher_runtime_env_contract(tmp_path, monkeypatch):
    from scripts.launch_v2 import load_runtime_env
    path = tmp_path / ".supabase-runtime.env.local"
    path.write_text(
        "export SUPABASE_RUNTIME_DB_URL='postgresql://runtime-only'\n"
        "CLINIC_WRITE_BACKEND=supabase\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    monkeypatch.delenv("SUPABASE_RUNTIME_DB_URL", raising=False)
    monkeypatch.delenv("CLINIC_WRITE_BACKEND", raising=False)
    assert load_runtime_env(tmp_path) is True
    assert os.environ["SUPABASE_RUNTIME_DB_URL"] == "postgresql://runtime-only"
    assert os.environ["CLINIC_WRITE_BACKEND"] == "supabase"
    os.environ.pop("SUPABASE_RUNTIME_DB_URL", None)
    os.environ.pop("CLINIC_WRITE_BACKEND", None)


def test_production_launcher_refuses_supabase_without_runtime_url(tmp_path, monkeypatch):
    from scripts.launch_v2 import load_runtime_env
    path = tmp_path / ".supabase-runtime.env.local"
    path.write_text("CLINIC_WRITE_BACKEND=supabase\n", encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.delenv("SUPABASE_RUNTIME_DB_URL", raising=False)
    monkeypatch.delenv("CLINIC_WRITE_BACKEND", raising=False)
    with pytest.raises(RuntimeError, match="SUPABASE_RUNTIME_DB_URL"):
        load_runtime_env(tmp_path)


def test_production_routing_file_selects_supabase_and_explicit_rollback_wins(tmp_path, monkeypatch):
    from scripts.launch_v2 import load_runtime_env
    config = tmp_path / "config"
    config.mkdir()
    (config / "production_runtime.env").write_text(
        "CLINIC_DATA_BACKEND=supabase\nCLINIC_WRITE_BACKEND=supabase\n", encoding="utf-8"
    )
    secret = tmp_path / ".supabase-runtime.env.local"
    secret.write_text("SUPABASE_RUNTIME_DB_URL=postgresql://runtime-only\n", encoding="utf-8")
    secret.chmod(0o600)
    monkeypatch.delenv("SUPABASE_RUNTIME_DB_URL", raising=False)
    monkeypatch.delenv("CLINIC_DATA_BACKEND", raising=False)
    monkeypatch.delenv("CLINIC_WRITE_BACKEND", raising=False)
    load_runtime_env(tmp_path)
    assert os.environ["CLINIC_DATA_BACKEND"] == "supabase"
    assert os.environ["CLINIC_WRITE_BACKEND"] == "supabase"
    os.environ.pop("SUPABASE_RUNTIME_DB_URL", None)
    os.environ.pop("CLINIC_DATA_BACKEND", None)
    os.environ.pop("CLINIC_WRITE_BACKEND", None)

    monkeypatch.setenv("CLINIC_WRITE_BACKEND", "sqlite")
    load_runtime_env(tmp_path)
    assert os.environ["CLINIC_WRITE_BACKEND"] == "sqlite"


# ---------------------------------------------------------------------------------------------
# refresh_clinic_projection_preserving_identity: parity against ClinicStore._project()
# ---------------------------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE clinics(
  id INTEGER PRIMARY KEY, uuid TEXT NOT NULL DEFAULT '', clinic_name TEXT NOT NULL DEFAULT '',
  phone TEXT NOT NULL DEFAULT '', address TEXT NOT NULL DEFAULT '', phone_norm TEXT NOT NULL DEFAULT '',
  tel_match_key TEXT NOT NULL DEFAULT '', name_norm TEXT NOT NULL DEFAULT '', name_prefix TEXT NOT NULL DEFAULT '',
  address_norm TEXT NOT NULL DEFAULT '', medical_key TEXT NOT NULL DEFAULT '', prefecture TEXT NOT NULL DEFAULT '',
  medical_type TEXT NOT NULL DEFAULT '', base_json TEXT NOT NULL DEFAULT '{}', effective_json TEXT NOT NULL DEFAULT '{}',
  active INTEGER NOT NULL DEFAULT 0, designation_date TEXT NOT NULL DEFAULT '', recent_until TEXT NOT NULL DEFAULT '',
  registration_reason TEXT NOT NULL DEFAULT '', owner_equal INTEGER, age_probability REAL,
  hp_status TEXT NOT NULL DEFAULT 'UNRESEARCHED', hp_url TEXT NOT NULL DEFAULT '', hp_rank TEXT NOT NULL DEFAULT 'UNKNOWN',
  signal_count INTEGER NOT NULL DEFAULT 0, hot_status TEXT NOT NULL DEFAULT '通常',
  departments_json TEXT NOT NULL DEFAULT '[]', treatments_json TEXT NOT NULL DEFAULT '[]', signals_json TEXT NOT NULL DEFAULT '[]',
  first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, source_as_of_date TEXT NOT NULL DEFAULT '',
  is_new INTEGER NOT NULL DEFAULT 0, merged_into INTEGER, merge_hold INTEGER NOT NULL DEFAULT 0,
  maps_presence_status TEXT NOT NULL DEFAULT '', maps_profile_url TEXT NOT NULL DEFAULT '',
  maps_website_url TEXT NOT NULL DEFAULT '', maps_match_method TEXT NOT NULL DEFAULT '',
  maps_checked_at TEXT NOT NULL DEFAULT '', exclude_reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE research_results(clinic_id INTEGER PRIMARY KEY, result_json TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE manual_overrides(clinic_id INTEGER, field TEXT, value_json TEXT NOT NULL, source TEXT, note TEXT, updated_at TEXT,
  PRIMARY KEY(clinic_id, field));
CREATE TABLE change_history(id INTEGER PRIMARY KEY, clinic_id INTEGER, action TEXT NOT NULL, before_json TEXT NOT NULL,
  after_json TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE hp_pages(clinic_id INTEGER, url TEXT, page_json TEXT NOT NULL, checked_at TEXT NOT NULL, PRIMARY KEY(clinic_id,url));
CREATE TABLE source_records(id INTEGER PRIMARY KEY, clinic_id INTEGER, source TEXT, record_json TEXT, source_hash TEXT,
  row_number INTEGER, match_status TEXT, match_reason TEXT, match_score REAL, created_at TEXT);
CREATE TABLE research_jobs(id TEXT PRIMARY KEY, kind TEXT NOT NULL, options_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'PAUSED', max_searches INTEGER NOT NULL, search_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE research_job_items(job_id TEXT, clinic_id INTEGER, state TEXT NOT NULL DEFAULT 'PENDING',
  result TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', PRIMARY KEY(job_id,clinic_id));
CREATE TABLE search_usage(id INTEGER PRIMARY KEY, month TEXT NOT NULL, job_id TEXT, query_key TEXT NOT NULL,
  attempted_at TEXT NOT NULL);
CREATE TABLE search_cache(query_key TEXT PRIMARY KEY, query TEXT NOT NULL, result_json TEXT NOT NULL, searched_at TEXT NOT NULL);
CREATE TABLE import_batches(id TEXT PRIMARY KEY, source TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE google_maps_results(id INTEGER PRIMARY KEY, clinic_id INTEGER, batch_id TEXT NOT NULL, row_number INTEGER NOT NULL,
  result_json TEXT NOT NULL, maps_match_status TEXT NOT NULL, maps_match_method TEXT NOT NULL, maps_profile_url TEXT NOT NULL,
  maps_website_url TEXT NOT NULL, scraped_at TEXT NOT NULL, created_at TEXT NOT NULL);
"""


@pytest.fixture
def temp_store():
    from src.master.store import ClinicStore
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    os.remove(path)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL)
    conn.execute("PRAGMA user_version=4")
    conn.commit()
    conn.close()
    store = ClinicStore.__new__(ClinicStore)
    store.path = __import__("pathlib").Path(path)
    yield store
    os.remove(path)


def _insert_base_clinic(store, record, medical_key_value=""):
    import json as _json
    from src.master.store import now, dumps
    ts = now()
    with store.connect() as c:
        cid = c.execute(
            "INSERT INTO clinics(uuid,base_json,first_seen_at,last_seen_at) VALUES(?,?,?,?)",
            (record.get("uuid", ""), dumps(record), ts, ts),
        ).lastrowid
        if medical_key_value:
            c.execute("UPDATE clinics SET medical_key=? WHERE id=?", (medical_key_value, cid))
    return cid


def test_refresh_projection_parity_with_existing_project(temp_store):
    """The new split (identity_contract.refresh_clinic_projection_preserving_identity +
    resolve_medical_key_transition) must compute the SAME non-identity projection fields and
    the SAME medical_key as the existing, unmodified ClinicStore._project() for a record where
    no identity conflict exists -- proving the split is behavior-preserving before any live
    wiring is considered.
    """
    record = {
        "clinic_name": "テスト歯科", "phone": "03-1234-5678", "address": "東京都千代田区1-1-1",
        "prefecture": "東京都", "medical_type": "歯科", "facility_type": "歯科診療所", "status": "現存",
        "clinic_id": "東京都:1,2345", "as_of": "2026-01-01",
    }
    cid = _insert_base_clinic(temp_store, record)

    with temp_store.connect() as c:
        temp_store._project(c, cid)
        expected = dict(c.execute("SELECT * FROM clinics WHERE id=?", (cid,)).fetchone())

    with temp_store.connect() as c:
        row = c.execute("SELECT * FROM clinics WHERE id=?", (cid,)).fetchone()
        base = json.loads(row["base_json"])
        data = {**base, "uuid": row["uuid"], "manual_fields": []}
    fields = refresh_clinic_projection_preserving_identity(data)
    from src.master.matching import medical_key as mk
    candidate = mk({**data, **fields})
    resolved = resolve_medical_key_transition(row["medical_key"], candidate, authoritative=True)

    assert fields["clinic_name"] == expected["clinic_name"]
    assert fields["name_norm"] == expected["name_norm"]
    assert fields["phone_norm"] == expected["phone_norm"]
    assert fields["active"] == expected["active"]
    assert fields["hot_status"] == expected["hot_status"]
    assert fields["effective_json"] == json.loads(expected["effective_json"])
    assert resolved == expected["medical_key"]


def test_sqlite_write_adapter_insert_new_clinic_and_guard(temp_store):
    from src.repository.sqlite_write_adapter import SqliteClinicWriteRepository
    repo = SqliteClinicWriteRepository(temp_store)
    record = {"clinic_name": "新規クリニック", "clinic_id": "東京都:9,9999", "prefecture": "東京都", "medical_type": "医科"}

    non_authoritative_id = repo.insert_new_clinic(record, is_new=True, is_authoritative_official_source=False)
    with temp_store.connect() as c:
        row = c.execute("SELECT medical_key FROM clinics WHERE id=?", (non_authoritative_id,)).fetchone()
    assert row["medical_key"] == ""

    authoritative_id = repo.insert_new_clinic(record, is_new=False, is_authoritative_official_source=True)
    with temp_store.connect() as c:
        row = c.execute("SELECT medical_key FROM clinics WHERE id=?", (authoritative_id,)).fetchone()
    assert row["medical_key"] == "東京都:医科:99999"


def test_sqlite_write_adapter_refresh_projection_rejects_known_to_known(temp_store):
    from src.repository.sqlite_write_adapter import SqliteClinicWriteRepository
    repo = SqliteClinicWriteRepository(temp_store)
    record = {"clinic_name": "既存クリニック", "clinic_id": "東京都:1,1111", "prefecture": "東京都", "medical_type": "医科"}
    cid = _insert_base_clinic(temp_store, record, medical_key_value="東京都:医科:2222")
    with pytest.raises(IdentityContractError):
        repo.refresh_projection(cid, is_authoritative_official_source=False)


def test_sqlite_write_adapter_settings_rejects_disallowed_key(temp_store):
    from src.repository.sqlite_write_adapter import SqliteSettingsWriteRepository
    repo = SqliteSettingsWriteRepository(temp_store)
    with pytest.raises(ValueError):
        repo.set("tavily_api_key", "secret")
    repo.set("monthly_limit", 500)
    assert temp_store.setting("monthly_limit") == 500


def test_sqlite_write_adapter_search_two_phase_and_idempotent_cache(temp_store):
    from src.repository.sqlite_write_adapter import SqliteSearchWriteRepository
    with temp_store.connect() as c:
        c.execute("INSERT INTO settings VALUES('monthly_limit','900')")
    repo = SqliteSearchWriteRepository(temp_store)
    repo.reserve_attempt(None, "key1", "2026-10")
    assert repo.monthly_usage_count("2026-10") == 1
    repo.store_cache_result("key1", "query", [{"url": "a"}])
    repo.store_cache_result("key1", "query", [{"url": "b"}])  # idempotent re-store, no error
    assert repo.get_cached("key1") == [{"url": "b"}]


# ---------------------------------------------------------------------------------------------
# Supabase write adapter: SQL-shape / guard / no-silent-fallback tests against a fake connection
# ---------------------------------------------------------------------------------------------

class FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self._result = None
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, query, params=None):
        self._conn.executed.append((str(query), params))
        self._result = self._conn.script.pop(0) if self._conn.script else None

    def executemany(self, query, seq):
        for params in seq:
            self.execute(query, params)

    def fetchone(self):
        return self._result

    def fetchall(self):
        return self._result or []


class FakeConn:
    def __init__(self, script=None):
        self.script = list(script or [])
        self.executed = []
        self.committed = 0
        self.rolled_back = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1


def test_supabase_settings_upsert_uses_on_conflict():
    from src.repository.supabase_write_adapter import SupabaseSettingsWriteRepository
    conn = FakeConn()
    SupabaseSettingsWriteRepository(conn).set("monthly_limit", 1000)
    sql_text, params = conn.executed[0]
    assert "ON CONFLICT(key) DO UPDATE" in sql_text
    assert conn.committed == 1 and conn.rolled_back == 0


def test_supabase_settings_rejects_disallowed_key_without_touching_connection():
    from src.repository.supabase_write_adapter import SupabaseSettingsWriteRepository
    conn = FakeConn()
    with pytest.raises(ValueError):
        SupabaseSettingsWriteRepository(conn).set("tavily_api_key", "secret")
    assert conn.executed == [] and conn.committed == 0


def test_supabase_insert_new_clinic_enforces_high_range_guard_and_rolls_back():
    from src.repository.supabase_write_adapter import SupabaseClinicWriteRepository
    from src.repository.high_range_id import HighRangeIdViolation
    conn = FakeConn(script=[(42,)])  # simulated RETURNING id below the reserved floor
    repo = SupabaseClinicWriteRepository(conn)
    with pytest.raises(HighRangeIdViolation):
        repo.insert_new_clinic({"clinic_name": "x"}, is_new=True, is_authoritative_official_source=False)
    assert conn.rolled_back == 1
    assert conn.committed == 0  # no silent fallback, no partial commit


def test_supabase_insert_new_clinic_commits_when_id_is_in_range():
    from src.repository.supabase_write_adapter import SupabaseClinicWriteRepository
    conn = FakeConn(script=[(1_000_000_005,)])
    repo = SupabaseClinicWriteRepository(conn)
    new_id = repo.insert_new_clinic({"clinic_name": "x"}, is_new=True, is_authoritative_official_source=False)
    assert new_id == 1_000_000_005
    assert conn.committed == 1 and conn.rolled_back == 0


def test_supabase_source_record_insert_uses_returning_and_guard():
    from src.repository.supabase_write_adapter import SupabaseProvenanceWriteRepository
    conn = FakeConn(script=[(1_000_000_010,)])
    repo = SupabaseProvenanceWriteRepository(conn)
    new_id = repo.insert_source_record(None, "厚生局", {"a": 1}, "hash", 1, "NEW", "reason", 1.0)
    assert new_id == 1_000_000_010
    sql_text, _ = conn.executed[0]
    assert "RETURNING id" in sql_text


def test_supabase_search_cache_upsert_idempotent_sql_shape():
    from src.repository.supabase_write_adapter import SupabaseSearchWriteRepository
    conn = FakeConn()
    repo = SupabaseSearchWriteRepository(conn)
    repo.store_cache_result("key1", "q", [{"url": "a"}])
    sql_text, _ = conn.executed[0]
    assert "ON CONFLICT(query_key) DO UPDATE" in sql_text
    assert conn.committed == 1


def test_supabase_refresh_projection_binds_postgres_booleans_as_bool():
    """SQLite's projection helper returns 0/1; psycopg must receive real booleans."""
    from src.repository.supabase_write_adapter import SupabaseClinicWriteRepository

    conn = FakeConn(script=[
        ({"clinic_name": "x", "status": "営業中", "facility_type": "診療所",
          "owner_name": "山田 太郎", "manager_name": "山田 太郎"}, "", ""),
        None,
        [],
    ])
    SupabaseClinicWriteRepository(conn).refresh_projection(1_000_000_000)
    update = next((query, params) for query, params in conn.executed if query.startswith("UPDATE public.clinics SET"))
    query, params = update
    columns = [piece.split("=")[0] for piece in query.removeprefix("UPDATE public.clinics SET ").split(" WHERE")[0].split(",")]
    bound = dict(zip(columns, params[:-1]))
    assert bound["active"] is True
    assert bound["owner_equal"] is True


def test_supabase_hp_pages_replace_uses_delete_then_on_conflict_insert():
    from src.repository.supabase_write_adapter import SupabaseResearchWriteRepository
    conn = FakeConn(script=[
        None, None, None, None, None,
        ({"clinic_name": "x", "status": "営業中", "facility_type": "診療所"}, "", ""),
        ('{"hp_status":"VERIFIED"}',), [],
    ])  # existing result + writes, then the canonical projection-refresh reads
    repo = SupabaseResearchWriteRepository(conn)
    repo.save_research(1_000_000_000, {"hp_status": "VERIFIED"}, pages=[{"url": "https://x", "title": "x"}])
    statements = [s for s, _ in conn.executed]
    assert any("DELETE FROM research.hp_pages" in s for s in statements)
    assert any("ON CONFLICT(clinic_id,url) DO UPDATE" in s for s in statements)
    assert any(s.startswith("UPDATE public.clinics SET") for s in statements)
    assert conn.committed == 1


def test_supabase_job_claim_uses_for_update_skip_locked():
    from src.repository.supabase_write_adapter import SupabaseJobsWriteRepository
    conn = FakeConn(script=[(1_000_000_000,)])
    repo = SupabaseJobsWriteRepository(conn)
    clinic_id = repo.claim_next_pending_item("job1")
    assert clinic_id == 1_000_000_000
    sql_text, _ = conn.executed[0]
    assert "FOR UPDATE SKIP LOCKED" in sql_text and "RETURNING clinic_id" in sql_text


def test_supabase_claim_returns_none_when_queue_empty():
    from src.repository.supabase_write_adapter import SupabaseJobsWriteRepository
    conn = FakeConn(script=[None])
    repo = SupabaseJobsWriteRepository(conn)
    assert repo.claim_next_pending_item("job1") is None
    assert conn.committed == 1


def test_supabase_jobs_runtime_methods_have_postgres_implementations(monkeypatch):
    from src.master.filters import Filters
    from src.repository.supabase_write_adapter import SupabaseJobsWriteRepository
    import src.repository.supabase_filters as pg_filters

    monkeypatch.setattr(pg_filters, "where", lambda filters: ("merged_into IS NULL", []))
    conn = FakeConn(script=[[(1_000_000_000,), (1_000_000_001,)]])
    repo = SupabaseJobsWriteRepository(conn)
    job_id = repo.create_job_from_filters(Filters(active_only=False, hp_only=False), limit=2)
    assert isinstance(job_id, str) and len(job_id) == 32
    statements = [sql for sql, _ in conn.executed]
    assert any("INSERT INTO research.research_jobs" in sql for sql in statements)
    assert sum("INSERT INTO research.research_job_items" in sql for sql in statements) == 2

    for invoke, expected in [
        (lambda r: r.complete_job_if_no_remaining_items("j"), "status='COMPLETED'"),
        (lambda r: r.claim_specific_item("j", 1), "state='RUNNING'"),
        (lambda r: r.requeue_item_for_budget_or_pause("j", 1, "stop", "PAUSED"), "state='PENDING'"),
        (lambda r: r.finish_item("j", 1, "SUCCESS", ""), "state='DONE'"),
    ]:
        branch = FakeConn()
        invoke(SupabaseJobsWriteRepository(branch))
        assert any(expected in sql for sql, _ in branch.executed)
        assert branch.committed == 1 and branch.rolled_back == 0


def test_supabase_recover_job_preserves_canonical_transition_order():
    from src.repository.supabase_write_adapter import SupabaseJobsWriteRepository

    class NamedCursor(FakeCursor):
        @property
        def description(self):
            return [type("Column", (), {"name": name}) for name in
                    ("id", "kind", "options_json", "status", "max_searches", "search_count", "created_at", "updated_at")]

    class NamedConn(FakeConn):
        def cursor(self):
            return NamedCursor(self)

    conn = NamedConn(script=[("job", "hp", {}, "PAUSED", 10, 0, "t", "t")])
    job = SupabaseJobsWriteRepository(conn).recover_job_for_run("job")
    assert job["status"] == "PAUSED"
    statements = [sql for sql, _ in conn.executed]
    assert "FOR UPDATE" in statements[0]
    assert "state='PENDING'" in statements[1]
    assert "status='PAUSED'" in statements[2]
    assert "status='RUNNING'" in statements[3]


def test_normal_runtime_modules_do_not_construct_sqlite_write_adapters():
    import inspect
    import src.master.jobs as jobs
    import src.enrichment.search_provider as search_provider
    import src.master.hp_research_batch as hp_batch
    from scripts import research_worker
    assert "SqliteJobsWriteRepository(" not in inspect.getsource(jobs)
    assert "SqliteResearchWriteRepository(" not in inspect.getsource(jobs)
    assert "SqliteSearchWriteRepository(" not in inspect.getsource(search_provider)
    assert "SqliteHpWriteRepository(" not in inspect.getsource(hp_batch)
    assert "SqliteTreatmentWriteRepository(" not in inspect.getsource(research_worker)
    assert "write_repositories_for" in inspect.getsource(jobs)
    assert "write_repositories_for" in inspect.getsource(search_provider)


def test_supabase_normal_runtime_methods_are_not_unsupported_stubs():
    import inspect
    from src.repository.supabase_write_adapter import SupabaseClinicWriteRepository, SupabaseJobsWriteRepository
    clinic_methods = ("import_comdesk", "import_master", "resolve_review", "refresh_age_model")
    job_methods = ("create_job_from_filters", "recover_job_for_run", "complete_job_if_no_remaining_items",
                   "claim_specific_item", "requeue_item_for_budget_or_pause", "finish_item")
    for cls, names in ((SupabaseClinicWriteRepository, clinic_methods), (SupabaseJobsWriteRepository, job_methods)):
        for name in names:
            source = inspect.getsource(getattr(cls, name))
            assert "BackendNotSupportedError" not in source
            assert "NotImplementedError" not in source


def test_supabase_write_failure_rolls_back_and_raises_not_falls_back(monkeypatch):
    """No silent WRITE fallback: a Postgres-side exception during a write must propagate,
    never be swallowed to retry on SQLite."""
    from src.repository.supabase_write_adapter import SupabaseSettingsWriteRepository

    class ExplodingCursor(FakeCursor):
        def execute(self, query, params=None):
            raise RuntimeError("simulated connection interruption")

    class ExplodingConn(FakeConn):
        def cursor(self):
            return ExplodingCursor(self)

    conn = ExplodingConn()
    with pytest.raises(RuntimeError):
        SupabaseSettingsWriteRepository(conn).set("monthly_limit", 1)
    assert conn.rolled_back == 1
    assert conn.committed == 0


# ---------------------------------------------------------------------------------------------
# import_maps_results: shared pure business logic (classify_match_status / build_maps_update)
# ---------------------------------------------------------------------------------------------

def test_supabase_adapter_reuses_the_exact_same_pure_helpers_as_sqlite_path():
    """Not a behavior test -- an identity (`is`) check that the Supabase adapter imports the
    SAME function objects google_maps.import_maps_results() calls, so the anti-downgrade
    business rule and counts classification cannot independently drift between backends."""
    import src.master.google_maps as gm
    from src.repository.supabase_write_adapter import SupabaseProvenanceWriteRepository
    import inspect
    source = inspect.getsource(SupabaseProvenanceWriteRepository.import_maps_results)
    assert "classify_match_status" in source and "build_maps_update" in source
    # And the module-level functions really are the ones google_maps.py defines (no shadow copy).
    assert gm.classify_match_status.__module__ == "src.master.google_maps"
    assert gm.build_maps_update.__module__ == "src.master.google_maps"


@pytest.mark.parametrize("status,expected_bucket", [
    ("MAPS_MATCHED_WEBSITE", "WEBSITE"), ("MAPS_MATCHED_NO_WEBSITE", "NO_WEBSITE"),
    ("MAPS_NOT_FOUND", "NOT_FOUND"), ("MAPS_AMBIGUOUS", "AMBIGUOUS"),
    ("EXCLUDED_HOSPITAL", "EXCLUDED"), ("ERROR", "ERROR"), ("", None),
])
def test_classify_match_status_buckets(status, expected_bucket):
    from src.master.google_maps import classify_match_status
    assert classify_match_status(status) == expected_bucket


def test_build_maps_update_preserves_confirmed_website_against_weaker_result():
    from src.master.google_maps import build_maps_update
    base = {"maps_presence_status": "MAPS_MATCHED_WEBSITE", "maps_website_url": "https://confirmed.example"}
    weaker_row = {"maps_website_url": "", "website_status": "NO_WEBSITE"}
    update, preserved = build_maps_update(weaker_row, base, "MAPS_MATCHED_NO_WEBSITE", "name_address")
    assert preserved is True
    assert update["maps_presence_status"] == "MAPS_MATCHED_WEBSITE"
    assert update["maps_website_url"] == "https://confirmed.example"


def test_build_maps_update_accepts_stronger_confirmed_website():
    from src.master.google_maps import build_maps_update
    base = {"maps_presence_status": "", "maps_website_url": ""}
    row = {"maps_website_url": "https://new.example", "website_status": "WEBSITE"}
    update, preserved = build_maps_update(row, base, "MAPS_MATCHED_WEBSITE", "name_address")
    assert preserved is False
    assert update["maps_website_url"] == "https://new.example"


def test_build_maps_update_does_not_preserve_when_incoming_is_also_confirmed():
    from src.master.google_maps import build_maps_update
    base = {"maps_presence_status": "MAPS_MATCHED_WEBSITE", "maps_website_url": "https://old.example"}
    row = {"maps_website_url": "https://new.example", "website_status": "WEBSITE"}
    update, preserved = build_maps_update(row, base, "MAPS_MATCHED_WEBSITE", "name_address")
    assert preserved is False
    assert update["maps_website_url"] == "https://new.example"


# ---------------------------------------------------------------------------------------------
# import_maps_results: SQLite ground-truth fixture (did not previously exist as a regression
# test anywhere in tests/ -- this both backfills coverage for the existing function and
# establishes the parity baseline the Supabase port above is checked against)
# ---------------------------------------------------------------------------------------------

def _maps_row(**overrides):
    from src.master.google_maps import MAPS_RESULT_HEADERS
    row = {h: "" for h in MAPS_RESULT_HEADERS}
    row.update(overrides)
    return row


def test_sqlite_import_maps_results_preserves_confirmed_website(temp_store):
    import pandas as pd
    from src.master.google_maps import import_maps_results
    cid = _insert_base_clinic(temp_store, {
        "clinic_name": "確認済みクリニック", "maps_presence_status": "MAPS_MATCHED_WEBSITE",
        "maps_website_url": "https://confirmed.example",
    })
    frame = pd.DataFrame([_maps_row(
        internal_clinic_id=str(cid), maps_match_status="MAPS_MATCHED_NO_WEBSITE",
        maps_website_url="", website_status="NO_WEBSITE",
    )])
    counts = import_maps_results(temp_store, frame)
    assert counts["PRESERVED_WEBSITE"] == 1
    assert counts["NO_WEBSITE"] == 1
    with temp_store.connect() as c:
        base = json.loads(c.execute("SELECT base_json FROM clinics WHERE id=?", (cid,)).fetchone()[0])
    assert base["maps_website_url"] == "https://confirmed.example"
    assert base["maps_presence_status"] == "MAPS_MATCHED_WEBSITE"


def test_sqlite_import_maps_results_accepts_new_website_match(temp_store):
    import pandas as pd
    from src.master.google_maps import import_maps_results
    cid = _insert_base_clinic(temp_store, {"clinic_name": "新規クリニック"})
    frame = pd.DataFrame([_maps_row(
        internal_clinic_id=str(cid), maps_match_status="MAPS_MATCHED_WEBSITE",
        maps_website_url="https://brand-new.example", website_status="WEBSITE",
    )])
    counts = import_maps_results(temp_store, frame)
    assert counts["WEBSITE"] == 1 and counts["PRESERVED_WEBSITE"] == 0
    with temp_store.connect() as c:
        base = json.loads(c.execute("SELECT base_json FROM clinics WHERE id=?", (cid,)).fetchone()[0])
    assert base["maps_website_url"] == "https://brand-new.example"


def test_sqlite_import_maps_results_batch_idempotent_on_retry(temp_store):
    import pandas as pd
    from src.master.google_maps import import_maps_results
    cid = _insert_base_clinic(temp_store, {"clinic_name": "重複チェック"})
    frame = pd.DataFrame([_maps_row(internal_clinic_id=str(cid), maps_match_status="MAPS_NOT_FOUND")])
    first = import_maps_results(temp_store, frame)
    second = import_maps_results(temp_store, frame)
    assert "already_imported" not in first
    assert second["already_imported"] is True
    assert second["NOT_FOUND"] == first["NOT_FOUND"]
    with temp_store.connect() as c:
        n = c.execute("SELECT count(*) FROM clinics WHERE id=? ", (cid,)).fetchone()[0]
        history_count = c.execute("SELECT count(*) FROM change_history WHERE clinic_id=?", (cid,)).fetchone()[0]
    assert n == 1
    # internal_clinic_id still resolves find_target to this clinic even though maps_match_status
    # says NOT_FOUND (the two signals are independent) -- so the first run writes one history
    # entry (maps_presence_status changes from "" to "MAPS_NOT_FOUND"); the idempotent retry
    # must not write a second one.
    assert history_count == 1


def test_sqlite_import_maps_results_unlinked_row_not_found(temp_store):
    import pandas as pd
    from src.master.google_maps import import_maps_results
    frame = pd.DataFrame([_maps_row(internal_clinic_id="999999", maps_match_status="MAPS_NOT_FOUND")])
    counts = import_maps_results(temp_store, frame)
    assert counts["UNLINKED"] == 1
    assert counts["NOT_FOUND"] == 1


# ---------------------------------------------------------------------------------------------
# import_maps_results: Supabase port, same fixtures, against a routing fake connection
# ---------------------------------------------------------------------------------------------

class RoutingFakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, query, params=None):
        q = " ".join(str(query).split())
        self._conn.executed.append((q, params))
        self._result = None
        for predicate, responder in self._conn.routes:
            if predicate(q, params):
                self._result = responder(params)
                return
        if "import_batches" in q and "SELECT" in q:
            self._result = None
            return
        raise AssertionError(f"RoutingFakeCursor: no route matched: {q} params={params}")

    def fetchone(self):
        return self._result[0] if (self._result and isinstance(self._result, list)) else self._result

    def fetchall(self):
        return self._result or []


class RoutingFakeConn:
    def __init__(self, routes):
        self.routes = routes
        self.executed = []
        self.committed = 0
        self.rolled_back = 0

    def cursor(self):
        return RoutingFakeCursor(self)

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1


def test_supabase_import_maps_results_preserves_confirmed_website():
    from src.repository.supabase_write_adapter import SupabaseProvenanceWriteRepository

    base = {"maps_presence_status": "MAPS_MATCHED_WEBSITE", "maps_website_url": "https://confirmed.example"}
    research_result_holder = {}

    def route_find_target(q, params):
        return "id=%s AND merged_into" in q

    def route_base_select(q, params):
        return "SELECT base_json FROM public.clinics WHERE id=%s FOR UPDATE" in q

    def route_projection_select(q, params):
        return "base_json,uuid,medical_key" in q

    routes = [
        (lambda q, p: "import_batches" in q and "SELECT" in q, lambda p: None),
        (route_find_target, lambda p: [(1_000_000_001,)]),
        (route_base_select, lambda p: [(dict(base),)]),
        (lambda q, p: "research.research_results" in q, lambda p: None),
        (lambda q, p: "manual_overrides" in q and "SELECT" in q, lambda p: []),
        (route_projection_select, lambda p: [(dict(base), "", "")]),
        (lambda q, p: q.startswith("INSERT") or q.startswith("UPDATE"), lambda p: None),
    ]
    conn = RoutingFakeConn(routes)
    repo = SupabaseProvenanceWriteRepository(conn)

    frame = __import__("pandas").DataFrame([_maps_row(
        internal_clinic_id="1000000001", maps_match_status="MAPS_MATCHED_NO_WEBSITE",
        maps_website_url="", website_status="NO_WEBSITE",
    )])
    counts = repo.import_maps_results(frame)
    assert counts["PRESERVED_WEBSITE"] == 1
    assert conn.committed == 1 and conn.rolled_back == 0
    update_calls = [p for q, p in conn.executed if q.startswith("UPDATE public.clinics SET base_json")]
    assert update_calls, "expected a base_json UPDATE to have been issued"


def test_supabase_import_maps_results_already_imported_is_idempotent_noop():
    from src.repository.supabase_write_adapter import SupabaseProvenanceWriteRepository
    prior_counts = {"TOTAL": 1, "MATCHED": 0, "WEBSITE": 0, "NO_WEBSITE": 0, "NOT_FOUND": 1,
                     "AMBIGUOUS": 0, "EXCLUDED": 0, "ERROR": 0, "UNLINKED": 1, "PRESERVED_WEBSITE": 0}
    routes = [(lambda q, p: "import_batches" in q and "SELECT" in q, lambda p: [(dict(prior_counts),)])]
    conn = RoutingFakeConn(routes)
    repo = SupabaseProvenanceWriteRepository(conn)
    frame = __import__("pandas").DataFrame([_maps_row(internal_clinic_id="42", maps_match_status="MAPS_NOT_FOUND")])
    result = repo.import_maps_results(frame)
    assert result["already_imported"] is True
    assert result["NOT_FOUND"] == 1
    # Idempotent retry must not issue any INSERT/UPDATE -- it's a pure read-and-return.
    assert not any(q.startswith("INSERT") or q.startswith("UPDATE") for q, _ in conn.executed)


# ---------------------------------------------------------------------------------------------
# Treatment worker Repositoryization (scripts/research_worker.py)
# ---------------------------------------------------------------------------------------------

def _treatment_status_row(research_status="DONE", candidate_count=1, **overrides):
    row = {
        "research_status": research_status, "candidate_count": candidate_count,
        "source_url": "https://a.example/", "final_url": "https://a.example/",
        "identity_verified": research_status == "DONE", "attempts": 1, "last_error": "",
        "taxonomy_version": "7A-v2", "evidence_engine_version": "phase7b-context-v3",
        "manifest_id": "phase7-fullrun-test0000", "git_commit_sha": "deadbeef",
        "researched_at": "2026-09-30T00:00:00+00:00",
    }
    row.update(overrides)
    return row


def _treatment_row(category="胃カメラ検査", status="CONFIRMED"):
    return {
        "treatment_category_name": category, "research_status": status,
        "matched_alias": "胃カメラ", "source_url": "https://a.example/",
        "page_title": "診療案内", "provider_context": "SELF_OFFER", "exclusion_context": "NONE",
        "evidence_engine_version": "phase7b-context-v3", "taxonomy_version": "7A-v2",
        "researched_at": "2026-09-30T00:00:00+00:00", "git_commit_sha": "deadbeef",
        "manifest_id": "phase7-fullrun-test0000",
    }


def test_sqlite_treatment_repo_delegates_to_write_clinic_atomic(tmp_path):
    from scripts import research_worker as worker
    from src.repository.treatment_sqlite_write_adapter import SqliteTreatmentWriteRepository
    db = worker.open_final_db(tmp_path / "final.sqlite3")
    repo = SqliteTreatmentWriteRepository(db)
    repo.write_clinic_result(42, [_treatment_row()], _treatment_status_row())
    got = db.execute(
        "SELECT research_status FROM clinic_treatment_research_final WHERE clinic_id=?", (42,)
    ).fetchone()
    assert got == ("CONFIRMED",)


def test_sqlite_treatment_repo_idempotent_same_clinic_twice(tmp_path):
    """same job item x2: identical input re-applied must not duplicate rows."""
    from scripts import research_worker as worker
    from src.repository.treatment_sqlite_write_adapter import SqliteTreatmentWriteRepository
    db = worker.open_final_db(tmp_path / "final.sqlite3")
    repo = SqliteTreatmentWriteRepository(db)
    row = _treatment_row()
    repo.write_clinic_result(1, [row], _treatment_status_row())
    repo.write_clinic_result(1, [row], _treatment_status_row())  # retry with identical input
    count = db.execute("SELECT count(*) FROM clinic_treatment_research_final WHERE clinic_id=?", (1,)).fetchone()[0]
    assert count == 1  # not 2


def test_sqlite_treatment_repo_retry_after_external_success_no_duplicate():
    """retry after external API success / unknown commit outcome: re-submitting the SAME
    result (as a client would on an ambiguous commit outcome) must not create a second
    business result -- DELETE+INSERT replace semantics make this true by construction."""
    import tempfile, os as _os
    from scripts import research_worker as worker
    from src.repository.treatment_sqlite_write_adapter import SqliteTreatmentWriteRepository
    fd, path = tempfile.mkstemp(suffix=".sqlite3"); _os.close(fd); _os.remove(path)
    db = worker.open_final_db(__import__("pathlib").Path(path))
    repo = SqliteTreatmentWriteRepository(db)
    rows = [_treatment_row("胃カメラ検査"), _treatment_row("大腸カメラ検査")]
    repo.write_clinic_result(7, rows, _treatment_status_row(candidate_count=2))
    # Simulate "client never received the commit ack" -> client retries the identical call.
    repo.write_clinic_result(7, rows, _treatment_status_row(candidate_count=2))
    count = db.execute("SELECT count(*) FROM clinic_treatment_research_final WHERE clinic_id=?", (7,)).fetchone()[0]
    assert count == 2  # exactly the 2 categories, not 4
    _os.remove(path)


def test_sqlite_treatment_repo_worker_restart_resumable_state_unaffected():
    """worker restart: a FETCH_FAILED write after a prior DONE must not clobber the prior
    successful Treatment rows (matches write_clinic_atomic's own documented invariant)."""
    import tempfile, os as _os
    from scripts import research_worker as worker
    from src.repository.treatment_sqlite_write_adapter import SqliteTreatmentWriteRepository
    fd, path = tempfile.mkstemp(suffix=".sqlite3"); _os.close(fd); _os.remove(path)
    db = worker.open_final_db(__import__("pathlib").Path(path))
    repo = SqliteTreatmentWriteRepository(db)
    repo.write_clinic_result(9, [_treatment_row()], _treatment_status_row(research_status="DONE"))
    # Process "restarts" and reprocesses clinic 9, this time hitting a transient fetch failure.
    repo.write_clinic_result(9, [], _treatment_status_row(research_status="FETCH_FAILED", candidate_count=0))
    still_there = db.execute(
        "SELECT research_status FROM clinic_treatment_research_final WHERE clinic_id=?", (9,)
    ).fetchone()
    assert still_there == ("CONFIRMED",)  # prior successful result preserved
    status = db.execute("SELECT research_status FROM clinic_research_status WHERE clinic_id=?", (9,)).fetchone()
    assert status == ("FETCH_FAILED",)  # but the attempt-level status reflects the latest attempt
    _os.remove(path)


class FakeTreatmentCursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, query, params=None):
        self._conn.executed.append((" ".join(str(query).split()), params))

    def executemany(self, query, seq):
        for params in seq:
            self.execute(query, params)

    def fetchone(self):
        return None  # no prior row by default (e.g. HP repo's attempts lookup)


class FakeTreatmentConn:
    def __init__(self):
        self.executed = []
        self.committed = 0
        self.rolled_back = 0

    def cursor(self):
        return FakeTreatmentCursor(self)

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1


def test_supabase_treatment_repo_done_deletes_then_inserts_and_upserts_status():
    from src.repository.treatment_supabase_write_adapter import SupabaseTreatmentWriteRepository
    conn = FakeTreatmentConn()
    repo = SupabaseTreatmentWriteRepository(conn)
    repo.write_clinic_result(42, [_treatment_row()], _treatment_status_row())
    statements = [q for q, _ in conn.executed]
    assert any(s.startswith("DELETE FROM treatment.clinic_treatment_research") for s in statements)
    assert any(s.startswith("INSERT INTO treatment.clinic_treatment_research") for s in statements)
    assert any("ON CONFLICT(clinic_id) DO UPDATE" in s for s in statements)
    assert conn.committed == 1 and conn.rolled_back == 0


def test_supabase_treatment_repo_fetch_failed_skips_delete_and_insert():
    from src.repository.treatment_supabase_write_adapter import SupabaseTreatmentWriteRepository
    conn = FakeTreatmentConn()
    repo = SupabaseTreatmentWriteRepository(conn)
    repo.write_clinic_result(9, [], _treatment_status_row(research_status="FETCH_FAILED", candidate_count=0))
    statements = [q for q, _ in conn.executed]
    assert not any("clinic_treatment_research" in s and "DELETE" in s for s in statements)
    assert any("clinic_research_status" in s for s in statements)
    assert conn.committed == 1


def test_supabase_treatment_repo_failure_rolls_back_not_falls_back():
    from src.repository.treatment_supabase_write_adapter import SupabaseTreatmentWriteRepository

    class ExplodingConn(FakeTreatmentConn):
        def cursor(self):
            class Boom:
                def __enter__(self2): return self2
                def __exit__(self2, *a): return False
                def execute(self2, *a, **k): raise RuntimeError("simulated failure")
            return Boom()

    conn = ExplodingConn()
    repo = SupabaseTreatmentWriteRepository(conn)
    with pytest.raises(RuntimeError):
        repo.write_clinic_result(1, [], _treatment_status_row())
    assert conn.rolled_back == 1 and conn.committed == 0


# ---------------------------------------------------------------------------------------------
# HP research batch worker Repositoryization (src/master/hp_research_batch.py)
# ---------------------------------------------------------------------------------------------

def _hp_result(clinic_id=1, fetch_status="OK", **overrides):
    row = {
        "clinic_id": clinic_id, "hp_url": "https://example.jp/", "fetch_status": fetch_status,
        "final_url": "https://example.jp/", "treatment_status": "DONE" if fetch_status == "OK" else "FETCH_FAILED",
        "treatment_categories": "[]", "hp_abc_candidate": "C", "hp_abc_score": "pos=2,neg=0",
        "candidate_rank_1": "", "candidate_rank_2": "", "ambiguity_reason": "", "feature_json": "[]",
        "researched_at": "2026-10-05T00:00:00+00:00", "engine_version": "test", "error_detail": "",
        "elapsed_seconds": 1.0,
    }
    row.update(overrides)
    return row


def test_sqlite_hp_repo_delegates_to_upsert_result(tmp_path):
    from src.master.hp_research_batch import connect_sidecar
    from src.repository.hp_sqlite_write_adapter import SqliteHpWriteRepository
    with connect_sidecar(tmp_path / "sidecar.sqlite3") as conn:
        repo = SqliteHpWriteRepository(conn)
        repo.upsert_result(_hp_result())
        row = conn.execute("SELECT attempts, fetch_status FROM hp_research_batch_results WHERE clinic_id=1").fetchone()
        assert row["attempts"] == 1 and row["fetch_status"] == "OK"


def test_sqlite_hp_repo_idempotent_same_item_twice_tracks_attempts(tmp_path):
    """same batch item x2 / retry after external success: attempts increments, no duplicate row."""
    from src.master.hp_research_batch import connect_sidecar
    from src.repository.hp_sqlite_write_adapter import SqliteHpWriteRepository
    with connect_sidecar(tmp_path / "sidecar.sqlite3") as conn:
        repo = SqliteHpWriteRepository(conn)
        result = _hp_result()
        repo.upsert_result(result)
        repo.upsert_result(result)  # worker restart / retry with identical input
        rows = conn.execute("SELECT * FROM hp_research_batch_results").fetchall()
        assert len(rows) == 1
        assert rows[0]["attempts"] == 2


def test_supabase_hp_repo_upsert_sql_shape_and_rename_and_jsonb():
    from src.repository.hp_supabase_write_adapter import SupabaseHpWriteRepository
    conn = FakeTreatmentConn()  # reused generic fake: records SQL text + params, tracks commit/rollback
    repo = SupabaseHpWriteRepository(conn)
    repo.upsert_result(_hp_result(treatment_categories='["内科"]', feature_json='["x"]'))
    statements = [q for q, _ in conn.executed]
    insert_stmt = next(s for s in statements if s.startswith("INSERT INTO hp_research.clinic_hp_research"))
    assert "machine_hp_rank" in insert_stmt and "hp_abc_candidate" not in insert_stmt
    assert "ON CONFLICT(clinic_id) DO UPDATE" in insert_stmt
    insert_params = next(p for q, p in conn.executed if q.startswith("INSERT INTO hp_research.clinic_hp_research"))
    from psycopg.types.json import Jsonb
    assert isinstance(insert_params[5], Jsonb)  # treatment_categories wrapped, not a raw string
    assert insert_params[-2] == 1  # attempts = prior_attempts(0) + 1
    assert conn.committed == 1


def test_supabase_hp_repo_failure_rolls_back_not_falls_back():
    from src.repository.hp_supabase_write_adapter import SupabaseHpWriteRepository

    class ExplodingConn(FakeTreatmentConn):
        def cursor(self):
            class Boom:
                def __enter__(self2): return self2
                def __exit__(self2, *a): return False
                def execute(self2, *a, **k): raise RuntimeError("simulated failure")
            return Boom()

    conn = ExplodingConn()
    repo = SupabaseHpWriteRepository(conn)
    with pytest.raises(RuntimeError):
        repo.upsert_result(_hp_result())
    assert conn.rolled_back == 1 and conn.committed == 0
