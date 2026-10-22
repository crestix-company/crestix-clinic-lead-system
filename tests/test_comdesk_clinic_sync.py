import json

from src.io.input_loader import load_table
from src.master.comdesk import COMDESK_EXPORT_HEADERS
from src.master.comdesk_sync import plan_sync, apply_sync
from src.master.store import ClinicStore, dumps, now
from src.repository.sqlite_write_adapter import SqliteClinicWriteRepository


def _table(**values):
    row = [""] * len(COMDESK_EXPORT_HEADERS)
    for header, value in values.items():
        row[COMDESK_EXPORT_HEADERS.index(header)] = value
    return load_table((",".join(COMDESK_EXPORT_HEADERS) + "\n" + ",".join(row) + "\n").encode(), "sync.csv")


def _clinic(store, uid="CLINIC-1", rank="B", url="https://old.example.jp"):
    effective = {"clinic_name": "検証医院", "phone": "03-1111-2222", "address": "東京都千代田区1-1",
                 "hp_url": url, "hp_rank": rank}
    with store.connect() as c:
        c.execute("INSERT INTO clinics(uuid,clinic_name,phone,address,hp_url,hp_rank,effective_json,base_json,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (uid, effective["clinic_name"], effective["phone"], effective["address"], url, rank,
                   dumps(effective), dumps(effective), now(), now()))
        clinic_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute("INSERT OR IGNORE INTO templates(id,headers_json,mapping_json,created_at) VALUES('test-template','[]','{}',?)", (now(),))
        c.execute("INSERT INTO comdesk_original_rows(clinic_id,template_id,row_json,uuid,source_hash,row_number,created_at) VALUES(?,?,?,?,?,?,?)",
                  (clinic_id, "test-template", "[]", "COMDESK-1", "test-source", clinic_id, now()))
        return clinic_id


def test_rank_update_email_unverified_blank_preservation_and_export_contract(tmp_path):
    store = ClinicStore(tmp_path / "isolated.sqlite")
    clinic_id = _clinic(store)
    repo = SqliteClinicWriteRepository(store)
    url_plan = plan_sync(_table(UUID="COMDESK-1", URL="https://new.example.jp"), repo)
    assert url_plan["rows"][0]["status"] == "HOLD"
    assert "URL" in url_plan["rows"][0]["reason"] or "hp_url" in url_plan["rows"][0]["reason"]
    table = _table(UUID="COMDESK-1", **{"HPランク": "C", "メールアドレス": "desk@example.jp"})
    plan = plan_sync(table, repo)
    assert plan["counts"] == {"AUTO_UPDATE": 1, "NO_CHANGE": 0, "HOLD": 0}
    assert plan["rows"][0]["changes"]["hp_rank"] == {"before": "B", "after": "C"}
    assert plan["rows"][0]["changes"]["email"]["after"] == "desk@example.jp"
    apply_sync(repo, plan)
    assert apply_sync(repo, plan) == {"already_applied": True, "updated": 1}
    with store.connect() as c:
        row = c.execute("SELECT uuid,hp_rank,hp_url,base_json FROM clinics WHERE id=?", (clinic_id,)).fetchone()
        assert row["uuid"] == "CLINIC-1"
        assert row["hp_rank"] == "C"
        assert row["hp_url"] == "https://old.example.jp"
        assert json.loads(row["base_json"])["comdesk_unverified_email"] == "desk@example.jp"
        assert c.execute("SELECT count(*) FROM change_history WHERE clinic_id=? AND action='Comdesk医院情報同期'", (clinic_id,)).fetchone()[0] == 1
    reopened = ClinicStore(tmp_path / "isolated.sqlite")
    persisted = SqliteClinicWriteRepository(reopened).comdesk_sync_clinics(["COMDESK-1"])["COMDESK-1"][0]
    assert persisted["hp_rank"] == "C"
    assert persisted["email"] == "desk@example.jp"
    assert plan_sync(_table(UUID="COMDESK-1", **{"HPランク": "C", "メールアドレス": "desk@example.jp"}), repo)["counts"]["NO_CHANGE"] == 1
    assert len(COMDESK_EXPORT_HEADERS) == 30


def test_empty_fields_duplicate_uuid_and_manual_rank_are_held_or_unchanged(tmp_path):
    store = ClinicStore(tmp_path / "isolated.sqlite")
    clinic_id = _clinic(store)
    repo = SqliteClinicWriteRepository(store)
    with store.connect() as c:
        c.execute("INSERT INTO manual_overrides(clinic_id,field,value_json,source,note,updated_at) VALUES(?,?,?,?,?,?)",
                  (clinic_id, "hp_rank", '"B"', "手動確認", "", now()))
    plan = plan_sync(_table(UUID="CLINIC-1", **{"HPランク": "C", "メールアドレス": ""}), repo)
    assert plan["counts"]["HOLD"] == 1
    assert plan["rows"][0]["changes"] == {}
    with store.connect() as c:
        c.execute("INSERT INTO clinics(uuid,clinic_name,effective_json,base_json,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?)",
                  ("CLINIC-2", "同UUID医院", "{}", "{}", now(), now()))
        other_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute("INSERT INTO comdesk_original_rows(clinic_id,template_id,row_json,uuid,source_hash,row_number,created_at) VALUES(?,?,?,?,?,?,?)",
                  (other_id, "test-template", "[]", "COMDESK-1", "duplicate-source", 1, now()))
    duplicate = plan_sync(_table(UUID="COMDESK-1", **{"HPランク": "C"}), repo)
    assert duplicate["rows"][0]["status"] == "HOLD"
    missing = plan_sync(_table(UUID="MISSING", **{"HPランク": "C"}), repo)
    assert missing["rows"][0]["status"] == "HOLD"
