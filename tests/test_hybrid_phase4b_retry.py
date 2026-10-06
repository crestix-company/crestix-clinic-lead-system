import sqlite3

from scripts.hybrid_phase4b_full_research import (
    ROBOTS_SAFE_ERROR,
    retry_robot_rows,
    retry_robots_plan,
    load_retry_target_ids,
)


def make_cache(path):
    with sqlite3.connect(path) as db:
        db.execute(
            """CREATE TABLE clinic_fetch(
                 clinic_id INTEGER PRIMARY KEY,
                 sampling_group TEXT NOT NULL,
                 fetch_status TEXT NOT NULL,
                 error TEXT NOT NULL)"""
        )
        db.executemany(
            "INSERT INTO clinic_fetch VALUES(?,?,?,?)",
            [
                (30, "NO_SIGNAL", "FETCH_FAILED", ROBOTS_SAFE_ERROR),
                (10, "NO_REPLAY_DATA", "FETCH_FAILED", ROBOTS_SAFE_ERROR),
                (20, "NO_SIGNAL", "FETCH_FAILED", "URLまたはDNSを確認できません。"),
                (40, "NO_SIGNAL", "ERROR", ROBOTS_SAFE_ERROR),
                (50, "NO_SIGNAL", "OK", ""),
            ],
        )


def test_retry_robot_rows_exactly_matches_approved_failure(tmp_path):
    cache = tmp_path / "cache.sqlite3"
    make_cache(cache)
    with sqlite3.connect(cache) as db:
        db.row_factory = sqlite3.Row
        assert [r["clinic_id"] for r in retry_robot_rows(db)] == [10, 30]


def test_retry_robots_plan_is_read_only_and_excludes_other_failures(tmp_path):
    cache = tmp_path / "cache.sqlite3"
    make_cache(cache)
    before = cache.read_bytes()

    plan = retry_robots_plan(cache)

    assert plan["mode"] == "DRY_RUN_READ_ONLY"
    assert plan["retry_target_count"] == 2
    assert plan["retry_target_group_counts"] == {"NO_REPLAY_DATA": 1, "NO_SIGNAL": 1}
    assert plan["excluded_other_failure_count"] == 2
    assert plan["first_clinic_id"] == 10
    assert plan["last_clinic_id"] == 30
    assert plan["cache_changed"] is False
    assert plan["http_started"] is False
    assert cache.read_bytes() == before


def test_retry_target_csv_rejects_duplicates(tmp_path):
    path = tmp_path / "targets.csv"
    path.write_text("clinic_id\n10\n10\n", encoding="utf-8")
    try:
        load_retry_target_ids(path)
        assert False, "expected duplicate rejection"
    except ValueError as exc:
        assert "duplicate" in str(exc)
