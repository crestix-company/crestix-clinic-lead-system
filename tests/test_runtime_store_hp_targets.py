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
    assert "FROM public.clinic_cloudrun_readonly c WHERE" in candidate_sql
    assert "FROM public.clinic_cloudrun_readonly c WHERE" in count_sql
    assert "NOT EXISTS (SELECT 1 FROM hp_research.clinic_hp_research h WHERE h.clinic_id=c.id)" in candidate_sql
    assert "COALESCE(c.hp_status,'')<>'VERIFIED'" in candidate_sql
    assert "COALESCE(c.hp_status,'')<>'VERIFIED'" in count_sql
    assert "uuid" not in candidate_sql.lower()
    assert "email_candidates" not in candidate_sql.lower()


def test_hp_candidate_preview_uses_projected_reader_relation_and_safe_columns():
    store, conn = _store([[ (101, "Clinic", "Tokyo", "医科", "https://example.test") ]])
    rows = store.maps_hp_candidate_preview("東京都", ["医科"], False, limit=10)

    assert rows == [{"id": 101, "clinic_name": "Clinic", "address": "Tokyo",
                     "medical_type": "医科", "maps_website_url": "https://example.test"}]
    sql, _args = conn.calls[0]
    assert "FROM public.clinic_cloudrun_readonly c" in sql
    assert "public.clinics" not in sql
    assert "email_candidates" not in sql


def test_status_candidates_use_hp_ledger_reader_and_always_exclude_reserved_jobs():
    store, conn = _store([[(7, "Clinic", "Tokyo", "医科", "https://clinic.test", "ERROR", "NOT_FOUND")]])
    rows = store.maps_hp_status_candidates("東京都", ["医科"], ["ERROR"], limit=100)

    assert rows == [{"id": 7, "clinic_name": "Clinic", "address": "Tokyo", "medical_type": "医科",
                     "maps_website_url": "https://clinic.test", "hp_status": "ERROR",
                     "hp_status_detail": "NOT_FOUND"}]
    sql, args = conn.calls[0]
    assert "LEFT JOIN hp_research.clinic_hp_research h ON h.clinic_id=c.id" in sql
    assert "queued_j.status IN ('RUNNING','PAUSED','BUDGET')" in sql
    assert "success_i.result::text ILIKE '%SUCCESS%'" not in sql
    assert "public.clinic_cloudrun_readonly c" in sql
    assert "public.clinics" not in sql and "email_candidates" not in sql.lower()
    assert "success_i.result::text ILIKE '%SUCCESS%'" not in sql
    assert args[-2:] == (["ERROR"], 100)


def test_status_candidate_query_rejects_unknown_states():
    store, _conn = _store([])
    with pytest.raises(ValueError, match="Unsupported"):
        store.maps_hp_status_candidates(statuses=["SUCCESS_WITH_WRITE"])


def test_status_candidate_query_can_show_success_read_only():
    store, conn = _store([[(7, "Clinic", "Tokyo", "医科", "https://clinic.test", "SUCCESS", "OK")]])
    rows = store.maps_hp_status_candidates(statuses=["SUCCESS"], limit=10)
    assert rows[0]["hp_status"] == "SUCCESS"
    sql, _args = conn.calls[0]
    assert "COALESCE(c.hp_status,'')<>'VERIFIED'" not in sql
    assert "h.fetch_status='OK' THEN 'SUCCESS'" in sql


def test_hp_job_result_preview_reads_only_reader_visible_projection_and_ledger():
    store, conn = _store([[(7, "Clinic", "Tokyo", "DONE", "SUCCESS", "OK",
                           "https://clinic.test/", "https://clinic.test/", "A", "HP確認済み", "A")]])
    rows = store.hp_normal_job_results("job-1")
    assert rows == [{
        "clinic_id": 7, "clinic_name": "Clinic", "address": "Tokyo", "item_state": "DONE",
        "item_result": "SUCCESS", "fetch_status": "OK", "hp_url": "https://clinic.test/",
        "final_url": "https://clinic.test/", "machine_hp_rank": "A",
        "published_hp_status": "HP確認済み", "published_hp_rank": "A",
    }]
    sql, params = conn.calls[0]
    assert "JOIN public.clinic_cloudrun_readonly c ON c.id=i.clinic_id" in sql
    assert "LEFT JOIN hp_research.clinic_hp_research h ON h.clinic_id=i.clinic_id" in sql
    assert "research.research_results" not in sql and "public.clinics" not in sql
    assert params == ("job-1",)


def test_hp_job_status_avoids_select_star_for_column_scoped_reader():
    from types import SimpleNamespace

    class JobCursor:
        def __init__(self, conn):
            self.conn = conn
            self.result = None
            self.description = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=()):
            normalized = " ".join(sql.split())
            self.conn.calls.append((normalized, tuple(params)))
            if normalized.startswith("SELECT id,kind,status,created_at FROM research.research_jobs"):
                self.description = [SimpleNamespace(name=n) for n in ("id", "kind", "status", "created_at")]
                self.result = ("job-1", "hp", "RUNNING", "today")
            elif "GROUP BY state" in normalized:
                self.result = [("PENDING", 5)]
            else:
                self.result = [("SUCCESS", 1)]

        def fetchone(self):
            return self.result

        def fetchall(self):
            return self.result

    class JobConnection:
        def __init__(self):
            self.calls = []

        def cursor(self):
            return JobCursor(self)

    conn = JobConnection()
    clinic_repo = type("ClinicRepo", (), {"_conn": conn})()
    store = SupabaseRuntimeStore(type("Repos", (), {"clinics": clinic_repo})())
    status = store.hp_job_status("job-1")

    assert status["id"] == "job-1" and status["status"] == "RUNNING"
    assert status["counts"] == {"PENDING": 5} and status["total"] == 5
    assert "SELECT *" not in conn.calls[0][0]
    assert "SELECT id,kind,status,created_at FROM research.research_jobs" in conn.calls[0][0]


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


def test_force_includes_researched_rows_but_excludes_published_success():
    predicate, args = _maps_hp_target_predicate("東京都", ["医科"], force=True)

    assert "hp_research.clinic_hp_research" not in predicate
    assert "COALESCE(c.hp_status,'')<>'VERIFIED'" in predicate
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
    assert "COALESCE(c.hp_status,'')<>'VERIFIED'" in predicate


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

    class AutoHp:
        def create_single_job(self, prefecture, medical_types, force, limit):
            self.created = (prefecture, medical_types, force, limit)
            return "job-test"

    store, auto_hp = RuntimeStore(), AutoHp()
    monkeypatch.setattr(app_v2, "write_repositories_for", lambda _store: type("R", (), {"auto_hp": auto_hp})())

    assert app_v2._create_maps_hp_job(store, "東京都", 50, False, ["医科"]) == "job-test"
    assert auto_hp.created == ("東京都", ["医科"], False, 50)


def test_sqlite_projection_is_not_used_to_guess_step4_research_completion():
    import app_v2

    class LocalStore:
        is_supabase_runtime = False

    assert app_v2._maps_hp_available_count(LocalStore(), "東京都", False, ["医科"]) == 0


def test_auto_run_read_counts_use_reader_projection_and_exclude_historical_success():
    store, conn = _store([(17,), (9,)])

    assert store.hp_auto_run_eligible_count("", ["医科", "歯科"]) == 17
    assert store.hp_auto_run_remaining_count({
        "prefecture": "", "medical_types": ["医科", "歯科"], "force": False, "run_id": "r1",
    }) == 9

    eligible_sql, eligible_args = conn.calls[0]
    remaining_sql, remaining_args = conn.calls[1]
    assert "FROM public.clinic_cloudrun_readonly c" in eligible_sql
    assert "FROM public.clinic_cloudrun_readonly c" in remaining_sql
    assert "prior_i.state='DONE'" in eligible_sql and "prior_i.result::text ILIKE '%%SUCCESS%%'" in eligible_sql
    assert "success_i.state='DONE'" in remaining_sql and "success_i.result::text ILIKE '%%SUCCESS%%'" in remaining_sql
    assert "public.clinics" not in eligible_sql + remaining_sql
    assert eligible_args == (["医科", "歯科"],)
    assert remaining_args == (["医科", "歯科"], "r1")
