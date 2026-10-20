"""Issue #126 follow-up (Phase 4, then a further review pass): the hospital/center
exclusion-ID query inside SupabaseClinicRepository._excluded_clinic_ids() takes ~14-15s
on the live Canary DB (measured directly against the production Supabase project,
read-only, on 2026-10-20 -- no row-level data retained here). It was previously cached
per repository INSTANCE only. runtime_store.py's own docstring on
_retry_closed_read_connection already documents that "a direct/session Postgres
connection can still be closed by the network or server while the app remains alive"
even though Streamlit's @st.cache_resource keeps the surrounding SupabaseRuntimeStore
alive for the whole process -- when that happens, a BRAND NEW SupabaseClinicRepository
instance is built with an empty instance-level cache, so every single rebuild re-pays
the full query.

The first cut of this fix added a module-level, TTL-bounded cache so a freshly rebuilt
repository instance reuses the previous result. Review of that version found three real
gaps, all covered below:
  1. The cache key was `read_only_ui` alone -- no notion of which DATABASE the cached
     result came from. Two connections to different databases under the same mode would
     collide. Fixed: the key now includes the connection's host/port/dbname/user.
  2. The instance-level cache had no expiry -- a long-lived instance would serve the
     same excluded-ID set forever. Fixed: both cache layers share the same TTL.
  3. Process-wide sharing is only safe for read_only_ui=True (provably never written to
     during a session). read_only_ui=False (normal/write mode, where a human can import
     Comdesk/厚生局 data or edit exclude_reason at any time, and nothing invalidates this
     cache on write) must never share results across repository instances/connections.
     Fixed: read_only_ui=False only ever uses its own instance-level cache.
This file also covers concurrent access: a cold/expired read_only_ui=True cache under
concurrent callers must trigger exactly one query (single-flight), not a thundering herd.
"""
import threading
import time

import pytest

from src.repository import supabase_adapter as sa
from src.repository.supabase_adapter import SupabaseClinicRepository


class _ConnInfo:
    def __init__(self, host="db.example.internal", port=5432, dbname="postgres", user="clinic_runtime"):
        self.host = host
        self.port = port
        self.dbname = dbname
        self.user = user


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
        if self.connection.on_query:
            self.connection.on_query()
        if "public.clinic_cloudrun_readonly" in normalized:
            self.rows = [(901,), (902,)]
        elif "public.clinics" in normalized:
            self.rows = [(801,)]
        else:
            raise AssertionError(f"Unexpected exclusion query: {normalized}")

    def fetchall(self):
        return list(self.rows)


class _Connection:
    def __init__(self, info=None, on_query=None):
        self.query_count = 0
        self.info = info or _ConnInfo()
        self.on_query = on_query

    def cursor(self):
        return _Cursor(self)


@pytest.fixture(autouse=True)
def _clear_process_cache():
    sa._excluded_ids_process_cache.clear()
    sa._excluded_ids_key_locks.clear()
    yield
    sa._excluded_ids_process_cache.clear()
    sa._excluded_ids_key_locks.clear()


def _repo(info=None, on_query=None):
    conn = _Connection(info=info, on_query=on_query)
    return SupabaseClinicRepository(conn, hp_repository=None), conn


# --- gap 3: read_only_ui=False never shares across instances -------------------------

def test_write_mode_never_shares_the_process_cache_across_instances():
    repo1, conn1 = _repo()
    first = repo1._excluded_clinic_ids(read_only_ui=False)
    assert first == [801]
    assert conn1.query_count == 1

    # A second, independently-built instance against the SAME database must still
    # re-query in write mode -- never reuse another instance's normal-mode result,
    # because nothing invalidates this cache on write (a human could have just
    # changed exclude_reason on the Mac app).
    repo2, conn2 = _repo()
    second = repo2._excluded_clinic_ids(read_only_ui=False)
    assert second == [801]
    assert conn2.query_count == 1  # re-queried, NOT served from another instance's cache

    assert sa._excluded_ids_process_cache == {}  # write mode never touches it at all


# --- read_only_ui=True: cross-instance reuse (the original fix) still works ----------

def test_read_only_mode_rebuilt_instance_reuses_the_process_cache():
    repo1, conn1 = _repo()
    first = repo1._excluded_clinic_ids(read_only_ui=True)
    assert first == [901, 902]
    assert conn1.query_count == 1

    # Simulates _reconnect_read() building a brand new repository instance after the
    # pooled connection was recycled, but still pointed at the SAME database.
    repo2, conn2 = _repo()
    second = repo2._excluded_clinic_ids(read_only_ui=True)
    assert second == [901, 902]
    assert conn2.query_count == 0  # no DB round-trip on the new instance


def test_normal_and_read_only_modes_are_cached_separately_and_asymmetrically():
    repo, conn = _repo()
    normal = repo._excluded_clinic_ids(read_only_ui=False)
    readonly = repo._excluded_clinic_ids(read_only_ui=True)
    assert normal == [801]
    assert readonly == [901, 902]
    assert conn.query_count == 2

    repo2, conn2 = _repo()
    assert repo2._excluded_clinic_ids(read_only_ui=True) == [901, 902]
    assert repo2._excluded_clinic_ids(read_only_ui=False) == [801]
    # read_only hit the process cache (0 queries); normal mode never does, so it re-queried.
    assert conn2.query_count == 1


def test_same_instance_still_uses_its_own_fast_path():
    repo, conn = _repo()
    repo._excluded_clinic_ids(read_only_ui=False)
    repo._excluded_clinic_ids(read_only_ui=False)
    repo._excluded_clinic_ids(read_only_ui=False)
    assert conn.query_count == 1


# --- gap 1: cache key must include connection/database identity ----------------------

def test_different_databases_under_the_same_mode_do_not_share_the_process_cache():
    repo1, conn1 = _repo(info=_ConnInfo(dbname="postgres_a"))
    first = repo1._excluded_clinic_ids(read_only_ui=True)
    assert first == [901, 902]
    assert conn1.query_count == 1

    repo2, conn2 = _repo(info=_ConnInfo(dbname="postgres_b"))
    second = repo2._excluded_clinic_ids(read_only_ui=True)
    assert second == [901, 902]  # same stub result, but it must be a FRESH query
    assert conn2.query_count == 1  # NOT served from postgres_a's cache entry

    assert len(sa._excluded_ids_process_cache) == 2  # two distinct cache entries


def test_different_hosts_under_the_same_mode_do_not_share_the_process_cache():
    repo1, conn1 = _repo(info=_ConnInfo(host="host-a"))
    repo1._excluded_clinic_ids(read_only_ui=True)
    repo2, conn2 = _repo(info=_ConnInfo(host="host-b"))
    repo2._excluded_clinic_ids(read_only_ui=True)
    assert conn1.query_count == 1
    assert conn2.query_count == 1


def test_same_database_same_host_still_shares_as_before():
    info = _ConnInfo()
    repo1, conn1 = _repo(info=info)
    repo1._excluded_clinic_ids(read_only_ui=True)
    repo2, conn2 = _repo(info=_ConnInfo())  # distinct object, identical field values
    repo2._excluded_clinic_ids(read_only_ui=True)
    assert conn1.query_count == 1
    assert conn2.query_count == 0


# --- gap 2: instance-level cache must also respect the TTL ---------------------------

def test_instance_level_cache_expires_after_its_ttl_even_without_a_rebuild(monkeypatch):
    """The SAME instance, never rebuilt, must still refresh after the TTL -- this is
    what "同一repositoryインスタンスのキャッシュがTTL期限切れ後も再利用される" was
    pointing at: previously there was no expiry at all for the instance-level cache."""
    repo, conn = _repo()
    repo._excluded_clinic_ids(read_only_ui=False)
    assert conn.query_count == 1

    real_monotonic = time.monotonic
    monkeypatch.setattr(
        sa.time, "monotonic",
        lambda: real_monotonic() + sa._EXCLUDED_IDS_CACHE_TTL_SECONDS + 1,
    )

    second = repo._excluded_clinic_ids(read_only_ui=False)
    assert second == [801]
    assert conn.query_count == 2  # expired -- the SAME instance must re-query


def test_instance_level_cache_expires_for_read_only_mode_too(monkeypatch):
    repo, conn = _repo()
    repo._excluded_clinic_ids(read_only_ui=True)
    assert conn.query_count == 1

    real_monotonic = time.monotonic
    monkeypatch.setattr(
        sa.time, "monotonic",
        lambda: real_monotonic() + sa._EXCLUDED_IDS_CACHE_TTL_SECONDS + 1,
    )

    # The process cache is also expired at this point (same TTL, same clock), so this
    # must fall through to a real re-query, not a stale process-cache hit.
    second = repo._excluded_clinic_ids(read_only_ui=True)
    assert second == [901, 902]
    assert conn.query_count == 2


def test_instance_level_cache_still_fast_within_the_ttl(monkeypatch):
    repo, conn = _repo()
    repo._excluded_clinic_ids(read_only_ui=False)
    real_monotonic = time.monotonic
    monkeypatch.setattr(sa.time, "monotonic", lambda: real_monotonic() + 1)
    repo._excluded_clinic_ids(read_only_ui=False)
    assert conn.query_count == 1  # well within TTL -- no re-query


# --- correctness invariant -------------------------------------------------------------

def test_process_cache_never_changes_the_returned_excluded_id_set():
    """The whole point is "don't change which clinics are excluded" -- this nails down
    that a cache-hit path and a cache-miss path return literally the same value."""
    repo1, _ = _repo()
    cache_miss_result = repo1._excluded_clinic_ids(read_only_ui=True)

    repo2, _ = _repo()
    cache_hit_result = repo2._excluded_clinic_ids(read_only_ui=True)

    assert cache_miss_result == cache_hit_result == [901, 902]


# --- gap 4: concurrent access ----------------------------------------------------------

def test_concurrent_cache_miss_triggers_exactly_one_query_not_a_thundering_herd():
    """Cloud Run's containerConcurrency can serve multiple requests in the same process
    at once. A cold/expired cache hit by several concurrent callers must not each pay
    the full ~14s query -- only one should, and the rest should wait and reuse it."""
    started = threading.Event()
    release = threading.Event()
    query_count = []

    def on_query():
        query_count.append(1)
        started.set()
        # Hold the "query" open until every thread has had a chance to arrive at the
        # lock, so a buggy (non-single-flight) implementation would have let them all
        # run the query concurrently instead of queueing behind the first one.
        release.wait(timeout=5)

    results = []
    errors = []

    def worker(info):
        try:
            repo, _ = _repo(info=info, on_query=on_query)
            results.append(repo._excluded_clinic_ids(read_only_ui=True))
        except Exception as exc:  # pragma: no cover - diagnostic only
            errors.append(exc)

    info = _ConnInfo()
    threads = [threading.Thread(target=worker, args=(info,)) for _ in range(6)]
    for t in threads:
        t.start()
    assert started.wait(timeout=5)
    release.set()
    for t in threads:
        t.join(timeout=5)

    assert not errors
    assert len(results) == 6
    assert all(r == [901, 902] for r in results)
    assert len(query_count) == 1  # exactly one real query ran, not six
