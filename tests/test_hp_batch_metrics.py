import sqlite3

import pytest

from src.master.hp_batch_metrics import BatchMetricInvariantError, web_research_metrics
from src.repository.supabase_adapter import SupabaseHpResearchRepository


def _production(path):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE clinics(id INTEGER PRIMARY KEY, hp_url TEXT, maps_website_url TEXT)")
        conn.executemany("INSERT INTO clinics VALUES(?,?,?)", [
            (1, "https://a.example", ""),
            (2, "", "https://b.example"),
            (3, "https://c.example", "https://c.example"),
            (4, "", "https://d.example"),
            (5, "", ""),
            (6, "https://e.example", ""),
        ])


def _batch(path, rows):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE hp_research_batch_results(clinic_id INTEGER PRIMARY KEY, fetch_status TEXT, treatment_categories TEXT)")
        conn.executemany("INSERT INTO hp_research_batch_results VALUES(?,?,?)", rows)


def test_web_research_metrics_and_both_invariants(tmp_path):
    production = tmp_path / "production.sqlite3"
    batch = tmp_path / "batch.sqlite3"
    _production(production)
    _batch(batch, [
        (1, "OK", '["x"]'), (2, "OK", '[]'), (3, "ERROR", '[]'),
        (4, "REVIEW", '[]'),
    ])

    result = web_research_metrics(production, batch)
    assert result == {
        "url_acquired": 5,
        "researched": 2,
        "failed": 1,
        "review": 1,
        "not_researched": 1,
        "treatment_detected": 1,
        "treatment_not_detected": 1,
    }
    assert result["url_acquired"] == result["researched"] + result["failed"] + result["review"] + result["not_researched"]
    assert result["researched"] == result["treatment_detected"] + result["treatment_not_detected"]


def test_missing_treatment_result_breaks_completion_invariant(tmp_path):
    production = tmp_path / "production.sqlite3"
    batch = tmp_path / "batch.sqlite3"
    _production(production)
    _batch(batch, [(1, "OK", None)])
    with pytest.raises(BatchMetricInvariantError, match="治療カテゴリ判定"):
        web_research_metrics(production, batch)


def test_missing_batch_sidecar_returns_none(tmp_path):
    production = tmp_path / "production.sqlite3"
    _production(production)
    assert web_research_metrics(production, tmp_path / "missing.sqlite3") is None


def test_supabase_hp_metrics_count_review_separately_and_select_by_history_row():
    class Cursor:
        sql = ""
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def execute(self, sql): self.sql = sql
        def fetchone(self): return (5, 2, 1, 1, 1, 1, 1)

    class Connection:
        def cursor(self):
            self.last_cursor = Cursor()
            return self.last_cursor

    connection = Connection()
    result = SupabaseHpResearchRepository(connection).batch_metrics(read_only_ui=True)
    assert result == {
        "url_acquired": 5, "researched": 2, "failed": 1, "review": 1,
        "not_researched": 1, "treatment_detected": 1, "treatment_not_detected": 1,
    }
    assert "public.clinic_cloudrun_readonly" in connection.last_cursor.sql
    assert "merged_into IS NULL" in connection.last_cursor.sql
    assert "history_clinic_id IS NULL" in connection.last_cursor.sql
    assert "fetch_status NOT IN ('OK','REVIEW')" in connection.last_cursor.sql
