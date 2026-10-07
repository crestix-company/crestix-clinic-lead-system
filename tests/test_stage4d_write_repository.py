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


def test_supabase_hp_pages_replace_uses_delete_then_on_conflict_insert():
    from src.repository.supabase_write_adapter import SupabaseResearchWriteRepository
    conn = FakeConn(script=[None])  # SELECT existing research_results -> none
    repo = SupabaseResearchWriteRepository(conn)
    repo.save_research(1_000_000_000, {"hp_status": "VERIFIED"}, pages=[{"url": "https://x", "title": "x"}])
    statements = [s for s, _ in conn.executed]
    assert any("DELETE FROM research.hp_pages" in s for s in statements)
    assert any("ON CONFLICT(clinic_id,url) DO UPDATE" in s for s in statements)
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


def test_import_maps_results_is_explicitly_unimplemented_not_silently_wrong():
    from src.repository.supabase_write_adapter import SupabaseProvenanceWriteRepository
    conn = FakeConn()
    with pytest.raises(NotImplementedError):
        SupabaseProvenanceWriteRepository(conn).import_maps_results(1, {}, "hash")
