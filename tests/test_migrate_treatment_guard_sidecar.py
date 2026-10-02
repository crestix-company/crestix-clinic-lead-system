import sqlite3

from scripts.migrate_treatment_guard_sidecar import apply_drop_transaction


def test_apply_drop_recounts_only_affected_candidate_count(tmp_path, monkeypatch):
    path = tmp_path / "sidecar.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript("""
        CREATE TABLE clinic_treatment_research_final(
          clinic_id INTEGER, treatment_category_name TEXT, research_status TEXT, matched_alias TEXT);
        CREATE TABLE clinic_research_status(
          clinic_id INTEGER PRIMARY KEY, research_status TEXT, identity_verified INTEGER,
          candidate_count INTEGER, last_error TEXT);
        INSERT INTO clinic_treatment_research_final VALUES(1,'A','REVIEW','x');
        INSERT INTO clinic_treatment_research_final VALUES(1,'B','NOT_CONFIRMED','');
        INSERT INTO clinic_treatment_research_final VALUES(2,'C','CONFIRMED','c');
        INSERT INTO clinic_research_status VALUES(1,'DONE',1,99,'');
        INSERT INTO clinic_research_status VALUES(2,'FETCH_FAILED',0,77,'fetch failed');
        """)
    monkeypatch.setitem(__import__("scripts.migrate_treatment_guard_sidecar", fromlist=["EXPECTED"]).EXPECTED, "drop_rows", 1)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        result = apply_drop_transaction(db, [{"clinic_id": "1", "treatment_category": "A"}])
        assert result["deleted_rows"] == 1
        assert db.execute("SELECT candidate_count FROM clinic_research_status WHERE clinic_id=1").fetchone()[0] == 1
        untouched = db.execute(
            "SELECT research_status,identity_verified,candidate_count,last_error FROM clinic_research_status WHERE clinic_id=2"
        ).fetchone()
        assert tuple(untouched) == ("FETCH_FAILED", 0, 77, "fetch failed")
