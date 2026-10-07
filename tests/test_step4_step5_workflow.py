import json

import pytest

from src.master.jobs import _hp_ledger_payload, _research_one
from src.repository.runtime_store import SupabaseRuntimeStore


def test_hp_ledger_payload_marks_only_confirmed_success_as_ok():
    success = _hp_ledger_payload(10, {
        "research_status": "SUCCESS", "hp_status": "VERIFIED", "hp_verified": True,
        "hp_url": "https://clinic.example/", "treatment_categories": ["内科"],
    }, "SUCCESS")
    assert success["fetch_status"] == "OK"
    assert success["final_url"] == "https://clinic.example/"
    assert json.loads(success["treatment_categories"]) == ["内科"]
    assert success["hp_abc_candidate"] == ""  # does not rewrite HP rank
    assert success["attempts"] == 1
    assert success["elapsed_seconds"] == 0  # same runtime worker contract
    assert success["portal_name"] == ""


@pytest.mark.parametrize("status", ["REVIEW", "ERROR", "NOT_FOUND"])
def test_hp_ledger_payload_retains_terminal_non_success_attempt(status):
    record = _hp_ledger_payload(11, {"research_status": status, "hp_status": status}, status, "attempted")
    assert record["fetch_status"] == status
    assert record["treatment_status"] == "FETCH_FAILED"


def test_review_access_limited_result_keeps_review_status_not_error():
    record = _hp_ledger_payload(12, {
        "research_status": "REVIEW", "hp_status": "VERIFIED", "hp_verified": True,
        "hp_url": "https://clinic.example/", "hp_content_status": "ACCESS_RESTRICTED",
    }, "REVIEW")
    assert record["fetch_status"] == "REVIEW"
    assert record["final_url"] == "https://clinic.example/"
    assert record["treatment_status"] == "FETCH_FAILED"


@pytest.mark.parametrize("failure", [False, True])
def test_completed_hp_worker_writes_canonical_ledger_before_finishing(monkeypatch, failure):
    import src.repository.write_backend as backend

    events = []

    class Repo:
        def get_saved_research(self, clinic_id):
            return {}

        def save_research(self, clinic_id, result, pages):
            events.append(("research", clinic_id, result["research_status"]))

    class Jobs:
        def finish_item(self, job_id, clinic_id, status, note):
            events.append(("finish", clinic_id, status))

        def requeue_item_for_budget_or_pause(self, *args):
            raise AssertionError("terminal test should not requeue")

    class Hp:
        def upsert_result(self, result):
            events.append(("ledger", result["clinic_id"], result["fetch_status"]))

    monkeypatch.setattr(backend, "write_repositories_for", lambda _store: type(
        "Repositories", (), {"jobs": Jobs(), "research": Repo(), "hp": Hp()}
    )())

    class Store:
        def get(self, clinic_id):
            return {"id": clinic_id, "hp_url": "https://clinic.example/"}

    class Researcher:
        def run(self, *_args):
            if failure:
                raise RuntimeError("expected test failure")
            return ({"research_status": "SUCCESS", "hp_status": "VERIFIED",
                     "hp_verified": True, "hp_url": "https://clinic.example/"}, [])

    assert _research_one(Store(), "job", {"kind": "hp"}, {}, Researcher(), 12)
    assert events[-2][0] == "ledger"
    assert events[-1] == ("finish", 12, "ERROR" if failure else "SUCCESS")
    assert events[-2][2] == ("ERROR" if failure else "OK")


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        self.conn.calls.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.conn.rows.pop(0)


class _Conn:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []

    def cursor(self):
        return _Cursor(self)


def _runtime_store(conn):
    clinic_repo = type("ClinicRepo", (), {"_conn": conn})()
    return SupabaseRuntimeStore(type("Repos", (), {"clinics": clinic_repo})())


def test_latest_completed_hp_job_is_explicit_and_deterministically_ordered():
    conn = _Conn([("job-2", "2026-10-02", "2026-10-03", 9, 9)])
    job = _runtime_store(conn).latest_completed_hp_job()
    sql, params = conn.calls[0]
    assert job == {"id": "job-2", "created_at": "2026-10-02", "updated_at": "2026-10-03",
                   "target_count": 9, "done_count": 9}
    assert params == ()
    assert "j.kind='hp' AND j.status='COMPLETED'" in sql
    assert "count(*) FILTER (WHERE i.state<>'DONE')=0" in sql
    assert "ORDER BY j.updated_at DESC NULLS LAST,j.created_at DESC,j.id DESC" in sql


def test_step5_keeps_current_job_metrics_but_exports_cumulative_uuid_empty_backlog():
    conn = _Conn([
        (9, 9, 7, 2, [101, 102]),
        ([90, 101, 102],),
    ])
    result = _runtime_store(conn).hp_job_export_summary("latest-job")

    current_sql, current_params = conn.calls[0]
    waiting_sql, waiting_params = conn.calls[1]
    assert result == {
        "target_count": 9,
        "done_count": 9,
        "success_count": 7,
        "uuid_existing_count": 2,
        "export_ids": [90, 101, 102],
        "carryover_count": 1,
    }
    assert current_params == ('"facility_type":"([^"]*)"', "latest-job")
    for required in (
        "i.job_id=%s", "i.state='DONE'", "i.result='SUCCESS'", "h.fetch_status='OK'",
        "COALESCE(BTRIM(h.final_url),'')<>''", "COALESCE(BTRIM(c.uuid),'')=''",
        "c.merged_into IS NULL", "c.merge_hold=false", "exclude_reason IN ('hospital','center')",
        "c.clinic_name LIKE '%%病院%%'", "c.clinic_name LIKE '%%センター%%'",
    ):
        assert required in current_sql

    assert waiting_params == ('"facility_type":"([^"]*)"',)
    assert "JOIN research.research_job_items i ON i.clinic_id=c.id" in waiting_sql
    assert "JOIN research.research_jobs j ON j.id=i.job_id AND j.kind='hp'" in waiting_sql
    assert "i.state='DONE' AND i.result='SUCCESS'" in waiting_sql
    assert "array_agg(DISTINCT c.id ORDER BY c.id)" in waiting_sql
    assert "JOIN hp_research.clinic_hp_research h ON h.clinic_id=c.id" in waiting_sql
    assert "h.fetch_status='OK'" in waiting_sql
    assert "COALESCE(BTRIM(c.uuid),'')=''" in waiting_sql
    assert "c.merged_into IS NULL" in waiting_sql
    assert "c.merge_hold=false" in waiting_sql
    assert "uuid" in current_sql  # UUID is an export-only condition, never a Step4 target condition.


def test_missing_only_ledger_repository_never_overwrites_existing_rows():
    from src.repository.hp_supabase_write_adapter import SupabaseHpWriteRepository

    class InsertCursor(_Cursor):
        def execute(self, sql, params=()):
            self.conn.calls.append((" ".join(sql.split()), params))
            self.row = None if self.conn.conflict else (params[0],)

        def fetchone(self):
            return self.row

    class InsertConn(_Conn):
        def __init__(self, rows, conflict=False):
            super().__init__(rows)
            self.conflict = conflict

        def cursor(self):
            return InsertCursor(self)

    conn = InsertConn([])
    payload = _hp_ledger_payload(303, {
        "research_status": "REVIEW", "hp_status": "REVIEW", "hp_url": "",
        "treatment_categories": [],
    }, "REVIEW")
    inserted = SupabaseHpWriteRepository(conn).insert_missing_results([payload])
    sql, params = conn.calls[0]
    assert inserted == [303]
    assert "ON CONFLICT(clinic_id) DO NOTHING" in sql
    assert "DO UPDATE" not in sql
    assert params[0] == 303
    assert params[2] == "REVIEW"
    assert params[15] == 1
    conflict_conn = InsertConn([], conflict=True)
    assert SupabaseHpWriteRepository(conflict_conn).insert_missing_results([payload]) == []
    assert "DO NOTHING" in conflict_conn.calls[0][0]
