import sqlite3

import pytest

from src.master.hp_batch_metrics import BatchMetricInvariantError, web_research_metrics


def _production(path):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE clinics(id INTEGER PRIMARY KEY, hp_url TEXT, maps_website_url TEXT)")
        conn.executemany("INSERT INTO clinics VALUES(?,?,?)", [
            (1, "https://a.example", ""),
            (2, "", "https://b.example"),
            (3, "https://c.example", "https://c.example"),
            (4, "", "https://d.example"),
            (5, "", ""),
        ])


def _batch(path, rows):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE hp_research_batch_results(clinic_id INTEGER PRIMARY KEY, fetch_status TEXT, treatment_categories TEXT)")
        conn.executemany("INSERT INTO hp_research_batch_results VALUES(?,?,?)", rows)


def test_web_research_metrics_and_both_invariants(tmp_path):
    production = tmp_path / "production.sqlite3"
    batch = tmp_path / "batch.sqlite3"
    _production(production)
    _batch(batch, [(1, "OK", '["x"]'), (2, "OK", '[]'), (3, "ERROR", '[]')])

    result = web_research_metrics(production, batch)
    assert result == {
        "url_acquired": 4,
        "researched": 2,
        "failed": 1,
        "not_researched": 1,
        "treatment_detected": 1,
        "treatment_not_detected": 1,
    }
    assert result["url_acquired"] == result["researched"] + result["failed"] + result["not_researched"]
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
