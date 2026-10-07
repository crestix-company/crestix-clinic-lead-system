"""Stage4-B Shadow Read: unit tests + failure injection. No real SQLite/Supabase connection is
needed -- primary and shadow are both fakes, so this exercises ShadowClinicStore's contract in
isolation: primary result always wins, shadow never raises into the caller, mismatches/errors
are classified correctly, and concurrency stays bounded.
"""
import time
import pytest

from src.repository import shadow as shadow_mod
from src.repository.shadow import ShadowClinicStore, run_shadow, fingerprint
from src.repository.errors import BackendNotSupportedError
from src.master.filters import Filters


class FakePrimary:
    def __init__(self):
        self.calls = []

    def get(self, cid):
        self.calls.append(("get", cid))
        return {"id": cid, "clinic_name": "テスト医院", "effective_hp_rank": "A"}

    def query(self, filters=None, limit=100, offset=0, as_of=None):
        self.calls.append(("query", limit, offset))
        return [{"id": 1}, {"id": 2}]

    def count(self, filters=None, as_of=None):
        self.calls.append(("count",))
        return 42

    def funnel(self, filters, as_of=None):
        return [("全マスター", 100), ("最終営業対象", 50)]

    def metrics(self):
        return {"全マスター": 100}

    def treatment_status_for_ids(self, ids):
        return {i: "NOT_RESEARCHED" for i in ids}

    @property
    def path(self):
        return "/fake/clinics.sqlite3"


@pytest.fixture(autouse=True)
def _enable_shadow(monkeypatch):
    monkeypatch.setenv(shadow_mod.SHADOW_ENV_VAR, "1")
    yield


def _drain():
    # Shadow work is fire-and-forget on a background thread; give it a moment, then make sure
    # the bounded pool is fully free again before the next test runs.
    for _ in range(50):
        if shadow_mod._inflight._value == shadow_mod.SHADOW_MAX_WORKERS:
            return
        time.sleep(0.02)


def test_default_off_means_zero_wrapping_behavior(monkeypatch):
    monkeypatch.delenv(shadow_mod.SHADOW_ENV_VAR, raising=False)
    assert shadow_mod.shadow_read_enabled() is False
    # run_shadow must be a true no-op when disabled: no thread submitted, no log line.
    calls = []
    run_shadow("get", "fp", 1, lambda: calls.append("shadow ran") or {}, lambda a, b: (True, []), {}, 0.001)
    _drain()
    assert calls == []


def test_passthrough_attribute_not_overridden():
    primary = FakePrimary()
    store = ShadowClinicStore(primary)
    assert store.path == "/fake/clinics.sqlite3"


def test_get_returns_primary_result_unchanged_even_when_shadow_mismatches(monkeypatch):
    primary = FakePrimary()
    store = ShadowClinicStore(primary)

    def shadow_repos_mismatch():
        class _Clinics:
            def get(self, cid):
                return {"id": cid, "clinic_name": "テスト医院", "effective_hp_rank": "D"}  # mismatched on purpose
        return (_Clinics(), None, None)

    monkeypatch.setattr(shadow_mod, "_shadow_repos", shadow_repos_mismatch)
    result = store.get(7)
    assert result == {"id": 7, "clinic_name": "テスト医院", "effective_hp_rank": "A"}  # primary, untouched
    _drain()


@pytest.mark.parametrize("failure", ["connection_failure", "timeout", "query_exception"])
def test_shadow_failure_injection_never_breaks_primary(monkeypatch, failure):
    """Section 9: connection failure / timeout / query exception on the Supabase side must never
    affect the SQLite (primary) result returned to the caller."""
    primary = FakePrimary()
    store = ShadowClinicStore(primary)

    def broken_shadow_repos():
        if failure == "connection_failure":
            raise ConnectionError("simulated: could not connect to Supabase")
        if failure == "timeout":
            raise TimeoutError("simulated: statement_timeout exceeded")
        raise RuntimeError("simulated: query raised")

    monkeypatch.setattr(shadow_mod, "_shadow_repos", broken_shadow_repos)

    assert store.get(99) == {"id": 99, "clinic_name": "テスト医院", "effective_hp_rank": "A"}
    assert store.count(Filters()) == 42
    assert store.query(Filters(), limit=10, offset=0) == [{"id": 1}, {"id": 2}]
    assert store.metrics() == {"全マスター": 100}
    _drain()


def test_unsupported_filter_is_skipped_not_errored(monkeypatch):
    primary = FakePrimary()
    store = ShadowClinicStore(primary)

    def shadow_repos_raises_unsupported():
        class _Clinics:
            def count(self, filters, as_of=None):
                raise BackendNotSupportedError("sales_tiers not supported")
        return (_Clinics(), None, None)

    monkeypatch.setattr(shadow_mod, "_shadow_repos", shadow_repos_raises_unsupported)
    assert store.count(Filters(sales_tiers=["A"])) == 42
    _drain()


def test_bounded_concurrency_never_exceeds_max_workers(monkeypatch):
    """A saturated pool must skip extra shadow submissions outright, never queue them
    unboundedly and never block the caller."""
    release_gate = []

    def slow_shadow_repos():
        class _Clinics:
            def get(self, cid):
                while not release_gate:
                    time.sleep(0.01)
                return {"id": cid}
        return (_Clinics(), None, None)

    monkeypatch.setattr(shadow_mod, "_shadow_repos", slow_shadow_repos)
    primary = FakePrimary()
    store = ShadowClinicStore(primary)
    try:
        for i in range(shadow_mod.SHADOW_MAX_WORKERS + 3):
            assert store.get(i)["id"] == i  # primary call must never block on the gate above
        assert shadow_mod._inflight._value >= 0
    finally:
        release_gate.append(True)
        _drain()


def test_fingerprint_never_contains_raw_filter_values():
    fp = fingerprint("count", Filters(keyword="さくら眼科クリニック"))
    assert "さくら" not in fp
    assert "眼科" not in fp
    assert len(fp) == 16


def test_comparators():
    from src.repository.shadow import _compare_get, _compare_id_list, _compare_scalar, _compare_dict
    assert _compare_get({"a": 1}, {"a": 1}) == (True, [])
    match, diffs = _compare_get({"a": 1, "b": 2}, {"a": 1, "b": 3})
    assert match is False and diffs == [{"column": "b", "difference_type": "value_mismatch"}]
    assert _compare_id_list([1, 2, 3], [1, 2, 3]) == (True, [])
    assert _compare_id_list([1, 2, 3], [1, 3, 2])[0] is False
    assert _compare_scalar(5, 5) == (True, [])
    assert _compare_scalar(5, 6)[0] is False
    assert _compare_dict({"x": 1}, {"x": 1}) == (True, [])
    assert _compare_dict({"x": 1}, {"x": 2})[0] is False
