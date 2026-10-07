import pytest

from src.repository.runtime_store import (
    SupabaseRuntimeStore,
    _maps_hp_target_predicate,
)


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        self.conn.calls.append((" ".join(sql.split()), tuple(params)))
        self.result = self.conn.responses.pop(0)

    def fetchall(self):
        return self.result

    def fetchone(self):
        return self.result


class _Connection:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def cursor(self):
        return _Cursor(self)


def _store(responses):
    conn = _Connection(responses)
    return SupabaseRuntimeStore(type("Repos", (), {"clinics": type("ClinicRepo", (), {"_conn": conn})()})()), conn


def test_hp_target_count_and_candidates_share_the_exact_eligibility_predicate():
    store, conn = _store([[(10,), (20,)], (2,)])

    candidates = store.maps_hp_candidate_ids("東京都", ["医科"], force=False, limit=500)
    count = store.maps_hp_available_count("東京都", ["医科"], force=False)

    assert candidates == [10, 20]
    assert count == 2
    candidate_sql, candidate_args = conn.calls[0]
    count_sql, count_args = conn.calls[1]
    candidate_predicate = candidate_sql.split(" WHERE ", 1)[1].split(" ORDER BY ", 1)[0]
    count_predicate = count_sql.split(" WHERE ", 1)[1]
    assert candidate_predicate == count_predicate
    assert candidate_args[:-1] == count_args == ("東京都", ["医科"])
    assert "NOT EXISTS (SELECT 1 FROM hp_research.clinic_hp_research h WHERE h.clinic_id=c.id)" in candidate_sql
    assert "hp_status" not in candidate_sql
    assert "hp_status" not in count_sql


def test_hp_target_filter_preserves_all_scope_and_maps_conditions():
    predicate, args = _maps_hp_target_predicate("東京都", ["医科"], force=False)

    for required in (
        "c.merged_into IS NULL",
        "c.merge_hold=false",
        "c.active=true",
        "c.maps_presence_status='MAPS_MATCHED_WEBSITE'",
        "COALESCE(BTRIM(c.maps_website_url),'')<>''",
        "c.prefecture=%s",
        "c.medical_type=ANY(%s::text[])",
        "NOT EXISTS (SELECT 1 FROM hp_research.clinic_hp_research h WHERE h.clinic_id=c.id)",
    ):
        assert required in predicate
    assert args == ["東京都", ["医科"]]


def test_force_includes_researched_rows_without_using_projection_hp_status():
    predicate, args = _maps_hp_target_predicate("東京都", ["医科"], force=True)

    assert "hp_research.clinic_hp_research" not in predicate
    assert "hp_status" not in predicate
    assert "c.prefecture=%s" in predicate
    assert "c.medical_type=ANY(%s::text[])" in predicate
    assert args == ["東京都", ["医科"]]


@pytest.mark.parametrize("fetch_status", ["OK", "ERROR", "INVALID_URL"])
def test_any_hp_research_status_excludes_clinic_when_force_is_off(fetch_status):
    predicate, _args = _maps_hp_target_predicate("", None, force=False)

    # The anti-join intentionally has no fetch_status condition: existence alone means
    # the clinic has already been attempted and is not eligible unless force is enabled.
    assert "NOT EXISTS (SELECT 1 FROM hp_research.clinic_hp_research h WHERE h.clinic_id=c.id)" in predicate
    assert fetch_status not in predicate
    assert "fetch_status" not in predicate


def test_empty_optional_filters_are_supported_and_force_off_uses_hp_research_sot():
    predicate, args = _maps_hp_target_predicate("", None, force=False)

    assert args == []
    assert "c.prefecture=%s" not in predicate
    assert "c.medical_type=ANY" not in predicate
    assert "NOT EXISTS (SELECT 1 FROM hp_research.clinic_hp_research" in predicate
    assert "hp_status" not in predicate


def test_ui_count_wrapper_delegates_the_selected_scope_to_supabase_ledger():
    import app_v2

    class RuntimeStore:
        is_supabase_runtime = True

        def maps_hp_available_count(self, prefecture, medical_types, force):
            self.received = (prefecture, medical_types, force)
            return 9

    store = RuntimeStore()
    assert app_v2._maps_hp_available_count(store, "東京都", False, ["医科"]) == 9
    assert store.received == ("東京都", ["医科"], False)


def test_created_job_uses_the_supabase_candidate_ids(monkeypatch):
    import app_v2

    class RuntimeStore:
        is_supabase_runtime = True

        def maps_hp_candidate_ids(self, prefecture, medical_types, force, limit):
            self.received = (prefecture, medical_types, force, limit)
            return [101, 202]

    class Jobs:
        def create_job(self, ids, kind, options, max_searches):
            self.created = (ids, kind, options, max_searches)
            return "job-test"

    store, jobs = RuntimeStore(), Jobs()
    monkeypatch.setattr(app_v2, "write_repositories_for", lambda _store: type("R", (), {"jobs": jobs})())

    assert app_v2._create_maps_hp_job(store, "東京都", 50, False, ["医科"]) == "job-test"
    assert store.received == ("東京都", ["医科"], False, 50)
    assert jobs.created == ([101, 202], "hp", {"force": False, "max_pages": 20}, 0)


def test_sqlite_projection_is_not_used_to_guess_step4_research_completion():
    import app_v2

    class LocalStore:
        is_supabase_runtime = False

    assert app_v2._maps_hp_available_count(LocalStore(), "東京都", False, ["医科"]) == 0
