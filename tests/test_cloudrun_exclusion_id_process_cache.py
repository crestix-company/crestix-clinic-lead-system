"""Issue #126 follow-up (Phase 4): the hospital/center exclusion-ID query inside
SupabaseClinicRepository._excluded_clinic_ids() takes ~14-15s on the live Canary DB
(measured directly against the production Supabase project, read-only, on 2026-10-20 --
no row-level data retained here). It was previously cached per repository INSTANCE
only. runtime_store.py's own docstring on _retry_closed_read_connection already
documents that "a direct/session Postgres connection can still be closed by the
network or server while the app remains alive" even though Streamlit's
@st.cache_resource keeps the surrounding SupabaseRuntimeStore alive for the whole
process -- when that happens, a BRAND NEW SupabaseClinicRepository instance is built
with an empty instance-level cache, so every single rebuild re-pays the full query.

This adds a module-level, TTL-bounded cache so a freshly rebuilt repository instance
reuses the previous result instead of re-querying -- without changing which clinic IDs
are excluded (same predicate, same query, just not re-run on every reconnect).
"""
import time

import pytest

from src.repository import supabase_adapter as sa
from src.repository.supabase_adapter import SupabaseClinicRepository


class _Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        normalized = " ".join(str(sql).split())
        if "SET statement_timeout" in normalized:
            return
        self.connection.query_count += 1
        if "public.clinic_cloudrun_readonly" in normalized:
            self.rows = [(901,), (902,)]
        elif "public.clinics" in normalized:
            self.rows = [(801,)]
        else:
            raise AssertionError(f"Unexpected exclusion query: {normalized}")

    def fetchall(self):
        return list(self.rows)


class _Connection:
    def __init__(self):
        self.query_count = 0

    def cursor(self):
        return _Cursor(self)


@pytest.fixture(autouse=True)
def _clear_process_cache():
    sa._excluded_ids_process_cache.clear()
    yield
    sa._excluded_ids_process_cache.clear()


def _repo():
    conn = _Connection()
    return SupabaseClinicRepository(conn, hp_repository=None), conn


def test_a_rebuilt_repository_instance_reuses_the_process_cache_instead_of_requerying():
    repo1, conn1 = _repo()
    first = repo1._excluded_clinic_ids(read_only_ui=False)
    assert first == [801]
    assert conn1.query_count == 1

    # Simulates _reconnect_read() building a brand new repository instance after the
    # pooled connection was recycled -- the OLD instance's cache is gone, but the
    # process-level cache should still be warm.
    repo2, conn2 = _repo()
    second = repo2._excluded_clinic_ids(read_only_ui=False)
    assert second == [801]
    assert conn2.query_count == 0  # no DB round-trip on the new instance


def test_normal_and_read_only_modes_are_cached_separately():
    repo, conn = _repo()
    normal = repo._excluded_clinic_ids(read_only_ui=False)
    readonly = repo._excluded_clinic_ids(read_only_ui=True)
    assert normal == [801]
    assert readonly == [901, 902]
    assert conn.query_count == 2  # one query per mode, each only once

    repo2, conn2 = _repo()
    assert repo2._excluded_clinic_ids(read_only_ui=False) == [801]
    assert repo2._excluded_clinic_ids(read_only_ui=True) == [901, 902]
    assert conn2.query_count == 0  # both modes served from the process cache


def test_same_instance_still_uses_its_own_fast_path_without_touching_process_cache_helpers():
    repo, conn = _repo()
    repo._excluded_clinic_ids(read_only_ui=False)
    repo._excluded_clinic_ids(read_only_ui=False)
    repo._excluded_clinic_ids(read_only_ui=False)
    assert conn.query_count == 1


def test_process_cache_expires_after_its_ttl(monkeypatch):
    repo1, conn1 = _repo()
    repo1._excluded_clinic_ids(read_only_ui=False)
    assert conn1.query_count == 1

    real_monotonic = time.monotonic
    monkeypatch.setattr(
        sa.time, "monotonic",
        lambda: real_monotonic() + sa._EXCLUDED_IDS_PROCESS_CACHE_TTL_SECONDS + 1,
    )

    repo2, conn2 = _repo()
    second = repo2._excluded_clinic_ids(read_only_ui=False)
    assert second == [801]
    assert conn2.query_count == 1  # expired -- must re-query, not silently go stale forever


def test_process_cache_never_changes_the_returned_excluded_id_set():
    """The whole point is "don't change which clinics are excluded" -- this nails down
    that a cache-hit path and a cache-miss path return literally the same value."""
    repo1, _ = _repo()
    cache_miss_result = repo1._excluded_clinic_ids(read_only_ui=True)

    repo2, _ = _repo()
    cache_hit_result = repo2._excluded_clinic_ids(read_only_ui=True)

    assert cache_miss_result == cache_hit_result == [901, 902]
