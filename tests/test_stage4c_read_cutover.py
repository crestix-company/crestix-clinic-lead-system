import json
import time
from types import SimpleNamespace

import pytest

from src.repository.backend import active_backend
from src.repository.cutover import Stage4CClinicStore
from src.repository.errors import BackendNotSupportedError


class FakeSqlite:
    path = "/tmp/fake-stage4c-store"

    def __init__(self):
        self.writes = []

    def get(self, clinic_id):
        return {"id": clinic_id, "source": "sqlite"}

    def get_by_uuid(self, uuid):
        return {"id": 1, "uuid": uuid, "source": "sqlite"}

    def get_by_medical_key(self, key):
        return {"id": 1, "medical_key": key, "source": "sqlite"}

    def query(self, *args):
        return [{"id": 1}, {"id": 2}]

    def count(self, *args):
        return 2

    def funnel(self, *args):
        return [("final", 2)]

    def metrics(self):
        return {"all": 2}

    def treatment_status_for_ids(self, ids):
        return {cid: "FETCHED" for cid in ids}

    def save_research(self, *args):
        self.writes.append(("save_research", args))
        return "sqlite-write"


class FakeClinics:
    def get(self, clinic_id):
        return {"id": clinic_id, "source": "supabase"}

    def get_by_uuid(self, uuid):
        return {"id": 1, "uuid": uuid, "source": "supabase"}

    def get_by_medical_key(self, key):
        return {"id": 1, "medical_key": key, "source": "supabase"}

    def query(self, *args):
        return [{"id": 1}, {"id": 2}]

    def count(self, *args):
        return 2

    def funnel(self, *args):
        return [("final", 2)]

    def metrics(self):
        return {"all": 2}


def repos(clinics=None):
    return SimpleNamespace(
        clinics=clinics or FakeClinics(),
        treatment=SimpleNamespace(status_for_ids=lambda ids: {cid: "FETCHED" for cid in ids}),
        hp_research=SimpleNamespace(batch_metrics=lambda: {"researched": 2}),
    )


def test_stage4c_default_is_supabase_and_sqlite_flag_is_immediate_rollback(monkeypatch):
    monkeypatch.delenv("CLINIC_DATA_BACKEND", raising=False)
    assert active_backend() == "supabase"
    monkeypatch.setenv("CLINIC_DATA_BACKEND", "sqlite")
    assert active_backend() == "sqlite"


def test_primary_read_is_supabase_but_write_delegates_to_sqlite(monkeypatch):
    monkeypatch.setenv("CLINIC_READ_COMPARATOR_ENABLED", "0")
    sqlite = FakeSqlite()
    store = Stage4CClinicStore(sqlite, primary_repositories=repos())
    assert store.get(7) == {"id": 7, "source": "supabase"}
    assert store.save_research(7, {"ok": True}) == "sqlite-write"
    assert sqlite.writes == [("save_research", (7, {"ok": True}))]


@pytest.mark.parametrize("error", [
    ConnectionError("offline"), TimeoutError("statement timeout"), RuntimeError("query error"),
    BackendNotSupportedError("compatibility path"),
])
def test_primary_failures_fall_back_and_are_counted(monkeypatch, error):
    monkeypatch.setenv("CLINIC_READ_COMPARATOR_ENABLED", "0")

    class Failing:
        def count(self, *args):
            raise error

    store = Stage4CClinicStore(FakeSqlite(), primary_repositories=repos(Failing()))
    assert store.count() == 2
    assert store.fallback_count == 1


def test_fallback_log_has_required_fields(monkeypatch, tmp_path):
    monkeypatch.setenv("CLINIC_READ_COMPARATOR_ENABLED", "0")
    records = []
    monkeypatch.setattr("src.repository.cutover._log", records.append)

    class Failing:
        def count(self, *args):
            raise TimeoutError("secret details must not be logged")

    store = Stage4CClinicStore(FakeSqlite(), primary_repositories=repos(Failing()))
    assert store.count() == 2
    assert records == [{
        "event": "fallback", "operation": "count", "fallback_count": 1,
        "fallback_reason": "timeout", "error_category": "TimeoutError",
        "fingerprint": records[0]["fingerprint"],
    }]
    assert "secret" not in json.dumps(records)


def test_sqlite_comparator_does_not_block_primary(monkeypatch):
    monkeypatch.setenv("CLINIC_READ_COMPARATOR_ENABLED", "1")
    sqlite = FakeSqlite()

    def slow_count(*args):
        time.sleep(0.2)
        return 2

    sqlite.count = slow_count
    store = Stage4CClinicStore(sqlite, primary_repositories=repos())
    started = time.monotonic()
    assert store.count() == 2
    assert time.monotonic() - started < 0.1
