from pathlib import Path

from src.repository.runtime_store import SupabaseRuntimeStore


def _norm(sql):
    return " ".join(str(sql).split())


class _Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.sql = ""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        self.sql = _norm(sql)
        self.connection.queries.append((self.sql, params))

    def fetchone(self):
        if "ORDER BY j.updated_at DESC NULLS LAST" in self.sql:
            return self.connection.job_row
        if "SELECT count(*)," in self.sql and "WHERE i.job_id=%s" in self.sql:
            return self.connection.summary_row
        if "array_agg(DISTINCT c.id ORDER BY c.id)" in self.sql:
            return (list(self.connection.waiting_ids),)
        raise AssertionError(f"unexpected fetchone query: {self.sql}")


class _Connection:
    def __init__(
        self,
        *,
        job_row=None,
        summary_row=(500, 300, 250, 20, [10, 20]),
        waiting_ids=(10, 20, 30),
    ):
        self.job_row = job_row
        self.summary_row = summary_row
        self.waiting_ids = list(waiting_ids)
        self.queries = []

    def cursor(self):
        return _Cursor(self)


def _store(**kwargs):
    store = object.__new__(SupabaseRuntimeStore)
    store._conn = _Connection(**kwargs)
    return store


def test_latest_completed_hp_job_contract_stays_completed_only():
    store = _store(job_row=("done-job", "created", "updated", 500, 500))

    job = store.latest_completed_hp_job()

    assert job == {
        "id": "done-job",
        "created_at": "created",
        "updated_at": "updated",
        "target_count": 500,
        "done_count": 500,
    }
    sql = store._conn.queries[-1][0]
    assert "j.status='COMPLETED'" in sql
    assert "count(*) FILTER (WHERE i.state<>'DONE')=0" in sql
    assert "PAUSED" not in sql


def test_latest_exportable_hp_job_accepts_paused_with_pending_items():
    store = _store(job_row=("paused-job", "created", "updated", "PAUSED", 500, 300))

    job = store.latest_exportable_hp_job()

    assert job == {
        "id": "paused-job",
        "created_at": "created",
        "updated_at": "updated",
        "status": "PAUSED",
        "target_count": 500,
        "done_count": 300,
    }
    sql = store._conn.queries[-1][0]
    assert "j.status IN ('PAUSED','COMPLETED')" in sql
    assert "j.status='PAUSED' OR count(*) FILTER (WHERE i.state<>'DONE')=0" in sql


def test_paused_summary_counts_only_done_and_keeps_cumulative_waiting_list():
    store = _store(
        summary_row=(500, 300, 250, 20, [10, 20]),
        waiting_ids=(10, 20, 30),
    )

    summary = store.hp_job_export_summary("paused-job")

    assert summary == {
        "target_count": 500,
        "done_count": 300,
        "success_count": 250,
        "uuid_existing_count": 20,
        "export_ids": [10, 20, 30],
        "carryover_count": 1,
    }
    current_sql = store._conn.queries[0][0]
    waiting_sql = store._conn.queries[1][0]

    # Current PAUSED/COMPLETED job: PENDING/RUNNING never enter metrics/export ids.
    assert "j.status IN ('PAUSED','COMPLETED')" in current_sql
    assert "i.state='DONE'" in current_sql

    # ERROR and unresolved REVIEW are excluded by the positive-only eligibility contract.
    assert "i.result='SUCCESS'" in current_sql
    assert "i.result='REVIEW'" in current_sql
    assert "human_decision IN ('OFFICIAL','ORGANIZATION_PAGE','ACCESS_RESTRICTED')" in current_sql
    assert "AUTO_NAME_ONLY_MISMATCH" in current_sql

    # HP fetch/content, UUID, merge and hospital/center guards remain intact.
    assert "h.fetch_status='OK'" in current_sql
    assert "COALESCE(BTRIM(h.final_url),'')<>''" in current_sql
    assert "COALESCE(BTRIM(c.uuid),'')=''" in current_sql
    assert "c.merged_into IS NULL" in current_sql
    assert "c.merge_hold=false" in current_sql
    assert "c.exclude_reason IN ('hospital','center')" in current_sql

    # Existing cumulative waiting-list semantics intentionally stay job-status agnostic.
    assert "JOIN research.research_jobs j ON j.id=i.job_id AND j.kind='hp'" in waiting_sql
    assert "j.status" not in waiting_sql
    assert "i.state='DONE'" in waiting_sql
    assert "COALESCE(BTRIM(c.uuid),'')=''" in waiting_sql


def test_resume_then_more_done_is_visible_on_next_summary_without_consuming_waiting_list():
    store = _store(summary_row=(500, 300, 250, 20, [10]), waiting_ids=(10, 30))

    first = store.hp_job_export_summary("paused-job")
    assert first["export_ids"] == [10, 30]

    # Download/export does not mark anything consumed. On the next PAUSE, newly-DONE
    # UUID-empty clinics are simply present in the live cumulative query result.
    store._conn.summary_row = (500, 420, 360, 25, [10, 40])
    store._conn.waiting_ids = [10, 30, 40]
    second = store.hp_job_export_summary("paused-job")

    assert second["export_ids"] == [10, 30, 40]
    assert second["carryover_count"] == 1
    assert all(
        query.lstrip().startswith(("WITH ", "SELECT "))
        for query, _params in store._conn.queries
    )


def test_step5_ui_explicitly_explains_paused_export():
    source = Path("app_v2.py").read_text(encoding="utf-8")

    assert 'latest_exportable_hp_job' in source
    assert '一時停止時点までの調査完了分を出力できます。' in source
