import csv
import json
import sqlite3
from argparse import Namespace

from scripts.treatment_guard_offline_dry_run import run


def _db(path, schema, rows=()):
    with sqlite3.connect(path) as db:
        db.executescript(schema)
        for sql, values in rows:
            db.execute(sql, values)


def test_offline_dry_run_drops_only_ambiguous_cross_department_and_never_fetches(tmp_path, monkeypatch):
    clinic_db, research_db, mhlw_db = (tmp_path / name for name in ("clinics.db", "research.db", "mhlw.db"))
    cache_root, output = tmp_path / "cache", tmp_path / "out"
    cache_db = cache_root / "manifest" / "cache.sqlite3"
    cache_db.parent.mkdir(parents=True)
    _db(clinic_db, """CREATE TABLE clinics(
        id INTEGER PRIMARY KEY, clinic_name TEXT, merged_into INTEGER, merge_hold INTEGER,
        active INTEGER, uuid TEXT, first_seen_at TEXT, exclude_reason TEXT, effective_json TEXT)""", [
        ("INSERT INTO clinics VALUES(?,?,?,?,?,?,?,?,?)", (1, "眼科レーザー医院", None, 0, 1, "", "2026-01-01", "", "{}")),
        ("INSERT INTO clinics VALUES(?,?,?,?,?,?,?,?,?)", (2, "山王クリニック", None, 0, 1, "", "2026-01-01", "", "{}")),
        ("INSERT INTO clinics VALUES(?,?,?,?,?,?,?,?,?)", (3, "未キャッシュ医院", None, 0, 1, "", "2026-01-01", "", "{}")),
    ])
    _db(research_db, """
        CREATE TABLE clinic_treatment_research_final(
          clinic_id INTEGER, treatment_category_name TEXT, research_status TEXT,
          matched_alias TEXT, source_url TEXT);
        CREATE TABLE clinic_research_status(clinic_id INTEGER, research_status TEXT, identity_verified INTEGER);
    """, [
        ("INSERT INTO clinic_treatment_research_final VALUES(?,?,?,?,?)", (1, "下肢静脈瘤血管内治療", "REVIEW", "レーザー治療", "https://eye.example")),
        ("INSERT INTO clinic_treatment_research_final VALUES(?,?,?,?,?)", (2, "CPAP療法", "CONFIRMED", "CPAP", "https://sanno.example")),
        ("INSERT INTO clinic_treatment_research_final VALUES(?,?,?,?,?)", (3, "歯科インプラント", "REVIEW", "インプラント", "https://missing.example")),
        ("INSERT INTO clinic_research_status VALUES(?,?,?)", (1, "DONE", 1)),
        ("INSERT INTO clinic_research_status VALUES(?,?,?)", (2, "DONE", 1)),
        ("INSERT INTO clinic_research_status VALUES(?,?,?)", (3, "DONE", 1)),
    ])
    _db(mhlw_db, "CREATE TABLE clinic_mhlw_departments_final(clinic_id INTEGER, mhlw_department_name TEXT)", [
        ("INSERT INTO clinic_mhlw_departments_final VALUES(?,?)", (1, "眼科")),
        ("INSERT INTO clinic_mhlw_departments_final VALUES(?,?)", (2, "皮膚科")),
        ("INSERT INTO clinic_mhlw_departments_final VALUES(?,?)", (3, "整形外科")),
    ])
    _db(cache_db, """
        CREATE TABLE page_cache(clinic_id INTEGER,page_url TEXT,final_url TEXT,page_title TEXT,
          sanitized_text TEXT,fetch_status TEXT);
    """, [
        ("INSERT INTO page_cache VALUES(?,?,?,?,?,?)", (1, "https://eye.example", "", "診療", "眼科レーザー治療を行っています。", "OK")),
        ("INSERT INTO page_cache VALUES(?,?,?,?,?,?)", (2, "https://sanno.example", "", "診療", "睡眠時無呼吸症候群にCPAP治療を行います。", "OK")),
    ])

    def forbidden(*args, **kwargs):
        raise AssertionError("HTTP/network access is forbidden in offline dry-run")
    monkeypatch.setattr("socket.create_connection", forbidden)
    try:
        import requests.sessions
        monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    except ImportError:
        pass

    report = run(Namespace(clinic_db=str(clinic_db), research_db=str(research_db), mhlw_db=str(mhlw_db),
                           cache_root=str(cache_root), output_dir=str(output)))
    assert report["metrics"]["http_request_count"] == 0
    assert report["metrics"]["removed_treatment_rows"] == 1
    assert report["metrics"]["cache_missing_clinics"] == 1
    assert report["production_inputs_unchanged"] is True
    with (output / "treatment_diff.csv").open(encoding="utf-8-sig") as stream:
        rows = {int(row["clinic_id"]): row for row in csv.DictReader(stream)}
    assert rows[1]["guard_result"] == "DROP_AMBIGUOUS_CROSS_DEPARTMENT_ALIAS"
    assert rows[2]["guard_result"] == "KEEP_SPECIFIC_ALIAS"
    assert rows[3]["guard_result"] == "UNDETERMINED_CACHE_MISSING"
    assert json.loads((output / "report.json").read_text())["metrics"]["evidence_statuses_changed"] == 0
