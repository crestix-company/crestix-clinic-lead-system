from pathlib import Path
from io import BytesIO
import json
import sqlite3

import pytest
from filelock import FileLock
from openpyxl import load_workbook

from src.io.input_loader import load_table
from src.io.output_writer import csv_bytes
from src.master.comdesk import COMDESK_HEADERS
from src.master.filters import Filters
from src.master.matching import MasterMatch, match_record
from src.master.store import ClinicStore
from src.normalizer.phone import tel_match_key, normalize_phone


ALL = Filters(active_only=False, hp_only=False)


def comdesk(name="架空クリニック", phone="312345678", uid="UUID-001", address=""):
    row = [""] * 28
    row[0], row[2], row[6], row[9] = uid, name, address, phone
    row[15], row[18] = "  元の備考\n改行を保持  ", "=1+1"
    return load_table(csv_bytes(COMDESK_HEADERS, [row]), "comdesk.csv")


def official(name="架空クリニック", phone="03-1234-5678", code="master-1"):
    return {"clinic_name": name, "phone": phone, "clinic_id": code, "prefecture": "東京都",
        "address": "東京都架空区1-2-3", "medical_type": "医科", "facility_type": "診療所",
        "status": "現存", "manager_name": "見本 太郎", "as_of": "2026-09-01"}


def seed_separate(store, monkeypatch, records):
    # 旧版で同じ医院が新規追加されてしまった保存状態を再現する。
    with monkeypatch.context() as temporary:
        temporary.setattr("src.master.store.match_record", lambda *args, **kwargs: MasterMatch("NEW", [], "旧版で電話番号書式が不一致"))
        store.import_master(records)


@pytest.mark.parametrize("raw,expected", [
    ("03-1234-5678", "312345678"), ("0312345678", "312345678"), ("312345678", "312345678"),
    (312345678, "312345678"), ("(03) 1234 5678", "312345678"), ("０３－１２３４－５６７８", "312345678"),
    ("00-123", "0123"), ("0123", "123"), ("0", ""), (0, ""), ("", ""), (None, ""),
    ("不明", ""), ("03-1234-5678 内線99", "31234567899"), ("312345678.0", "3123456780"),
])
def test_key_exact_contract(raw, expected):
    assert tel_match_key(raw) == expected


def test_old_display_normalizer_is_unchanged():
    assert normalize_phone("03-1234-5678") == "0312345678"
    assert normalize_phone("312345678") == "312345678"


@pytest.mark.parametrize("master_first", [False, True])
def test_new_imports_match_both_orders_without_changing_original(master_first, tmp_path):
    store = ClinicStore(tmp_path / "new.db")
    incoming = comdesk()
    if master_first:
        store.import_master([official()])
    result = store.import_comdesk(incoming)
    if not master_first:
        result = store.import_master([official()])
    assert result["MATCHED"] == 1 and store.count(ALL) == 1
    record = store.query(ALL)[0]
    assert record["uuid"] == "UUID-001" and record["tel_match_key"] == "312345678"
    with store.connect() as connection:
        assert json.loads(connection.execute("SELECT row_json FROM comdesk_original_rows").fetchone()[0]) == incoming.data.iloc[0].tolist()
    for filename, content in store.export(ALL).items():
        output = load_table(content, filename)
        assert output.headers == COMDESK_HEADERS and output.value(0, 9) == "312345678"
        assert output.data.iloc[0].tolist() == incoming.data.iloc[0].tolist()


def test_phone_takes_priority_over_identity_conflict(tmp_path):
    store = ClinicStore(tmp_path / "priority.db")
    store.import_master([official()])
    store.import_master([official("別医院", "03-2222-2222", "master-2")])
    changed = official("別医院", "03-2222-2222", "master-1")
    with store.connect() as connection:
        result = match_record(connection, changed)
    assert result.status == "AMBIGUOUS" and len(result.candidates) == 2


def test_name_address_fallback_and_empty_phones_are_not_equal(tmp_path):
    store = ClinicStore(tmp_path / "fallback.db")
    store.import_comdesk(comdesk(phone="", address="東京都架空区1-2-3"))
    assert store.import_master([official()])["MATCHED"] == 1
    assert store.import_comdesk(comdesk(name="別の医院", phone="", uid="UUID-002"))["NEW"] == 1


def test_legacy_migration_reintegration_backup_and_repeat(tmp_path, monkeypatch):
    store = ClinicStore(tmp_path / "legacy.db")
    incoming = comdesk()
    store.import_comdesk(incoming)
    seed_separate(store, monkeypatch, [official(), official("新しい医院", "03-9999-9999", "master-2")])
    assert store.count(ALL) == 3
    with store.connect() as connection:
        original_sources = [tuple(row) for row in connection.execute("SELECT id,record_json FROM source_records ORDER BY id")]
        original_rows = [tuple(row) for row in connection.execute("SELECT id,row_json,uuid FROM comdesk_original_rows ORDER BY id")]
        connection.execute("DROP INDEX idx_clinic_tel_match")
        connection.execute("ALTER TABLE clinics DROP COLUMN tel_match_key")
        connection.execute("ALTER TABLE clinics DROP COLUMN merge_hold")
        connection.execute("PRAGMA user_version=2")
    store = ClinicStore(store.path)
    assert Path(str(store.path) + ".before-tel-key-v3.bak").exists()
    result = store.reintegrate_existing()
    assert {key: result[key] for key in ["MATCHED", "NEW", "AMBIGUOUS"]} == {"MATCHED": 1, "NEW": 1, "AMBIGUOUS": 0}
    assert result["merged_clinics"] == 1 and result["created_clinics"] == 0
    assert store.count(ALL) == 2
    assert ClinicStore(result["backup_path"]).count(ALL) == 3
    again = store.reintegrate_existing()
    assert again["merged_clinics"] == 0 and again["created_clinics"] == 0
    assert [again[key] for key in ["MATCHED", "NEW", "AMBIGUOUS"]] == [1, 1, 0]
    with store.connect() as connection:
        assert [tuple(row) for row in connection.execute("SELECT id,record_json FROM source_records ORDER BY id")] == original_sources
        assert [tuple(row) for row in connection.execute("SELECT id,row_json,uuid FROM comdesk_original_rows ORDER BY id")] == original_rows
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    files = store.export(ALL)
    for filename, content in files.items():
        output = load_table(content, filename)
        assert output.headers == COMDESK_HEADERS
        existing = next(row for row in output.data.values.tolist() if row[0] == "UUID-001")
        assert existing == incoming.data.iloc[0].tolist()
    book = load_workbook(BytesIO(files["final_comdesk_import.xlsx"]))
    assert book.active["S2"].value == "=1+1" and book.active["S2"].data_type == "s"


def test_previously_pending_source_is_reused(tmp_path, monkeypatch):
    store = ClinicStore(tmp_path / "pending.db")
    store.import_comdesk(comdesk())
    with monkeypatch.context() as temporary:
        temporary.setattr("src.master.store.match_record", lambda *args, **kwargs: MasterMatch("AMBIGUOUS", [1], "旧版の曖昧一致"))
        store.import_master([official()])
    assert len(store.reviews()) == 1
    result = store.reintegrate_existing()
    assert result["MATCHED"] == 1 and result["created_clinics"] == 0
    assert store.count(ALL) == 1 and not store.reviews()
    with store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM source_records").fetchone()[0] == 2


def test_shared_phone_stays_review_and_manual_separate_is_stable(tmp_path, monkeypatch):
    store = ClinicStore(tmp_path / "shared.db")
    store.import_comdesk(comdesk())
    store.import_comdesk(comdesk(uid="UUID-002"))
    store.resolve_review(store.reviews()[0]["id"], note="別UUIDとして保持")
    seed_separate(store, monkeypatch, [official()])
    result = store.reintegrate_existing()
    assert result["AMBIGUOUS"] == 1 and result["merged_clinics"] == 0
    review = store.reviews()[0]
    assert len(json.loads(review["candidates_json"])) == 2
    assert store.count(ALL) == 2  # 保留の厚生局側だけを営業対象から除く。
    with store.connect() as connection:
        source_id = connection.execute("SELECT clinic_id FROM source_records WHERE source='厚生局'").fetchone()[0]
    chosen = store.resolve_review(review["id"], note="電話を共有する別医院と確認")
    assert chosen == source_id and store.count(ALL) == 3
    assert store.reintegrate_existing()["NEW"] == 1
    assert store.count(ALL) == 3 and not store.reviews()


def test_shared_phone_manual_merge_keeps_both_distinct_uuids(tmp_path, monkeypatch):
    store = ClinicStore(tmp_path / "manual_merge.db")
    store.import_comdesk(comdesk())
    store.import_comdesk(comdesk(uid="UUID-002"))
    store.resolve_review(store.reviews()[0]["id"], note="別UUIDとして保持")
    seed_separate(store, monkeypatch, [official()])
    store.reintegrate_existing()
    target = next(row["id"] for row in store.query(ALL) if row["uuid"] == "UUID-001")
    store.resolve_review(store.reviews()[0]["id"], target, note="院長・住所を確認済み")
    assert {row["uuid"] for row in store.query(ALL)} == {"UUID-001", "UUID-002"}
    assert store.reintegrate_existing()["MATCHED"] == 1
    assert not store.reviews()


def test_manual_values_research_and_search_usage_survive(tmp_path, monkeypatch):
    store = ClinicStore(tmp_path / "research.db")
    store.import_comdesk(comdesk())
    seed_separate(store, monkeypatch, [official()])
    existing = next(row for row in store.query(ALL) if row["uuid"])
    official_row = next(row for row in store.query(ALL) if not row["uuid"])
    store.override(existing["id"], "hp_rank", "A", "手動確認")
    store.save_research(official_row["id"], {"hp_status": "VERIFIED", "hp_url": "https://demo.example/"})
    with store.connect() as connection:
        connection.execute("INSERT INTO search_usage(month,query_key,attempted_at) VALUES('2026-09','fixture','2026-09-14')")
    store.reintegrate_existing()
    result = store.get(existing["id"])
    assert result["hp_rank"] == "A" and result["hp_url"] == "https://demo.example/"
    assert store.get(official_row["id"])["id"] == existing["id"]
    with store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM search_usage").fetchone()[0] == 1


def test_conflicting_manual_values_remain_pending(tmp_path, monkeypatch):
    store = ClinicStore(tmp_path / "conflict.db")
    store.import_comdesk(comdesk())
    seed_separate(store, monkeypatch, [official()])
    records = store.query(ALL)
    store.override(records[0]["id"], "hp_rank", "A", "確認1")
    store.override(records[1]["id"], "hp_rank", "C", "確認2")
    result = store.reintegrate_existing()
    assert result["AMBIGUOUS"] == 1 and result["merged_clinics"] == 0
    assert len(store.reviews()) == 1


def test_completed_research_items_and_source_history_survive(tmp_path, monkeypatch):
    store = ClinicStore(tmp_path / "jobs.db")
    store.import_comdesk(comdesk())
    seed_separate(store, monkeypatch, [official()])
    existing = next(row["id"] for row in store.query(ALL) if row["uuid"])
    duplicate = next(row["id"] for row in store.query(ALL) if not row["uuid"])
    with store.connect() as connection:
        for job in ["both", "only_duplicate"]:
            connection.execute("INSERT INTO research_jobs(id,kind,options_json,max_searches,created_at,updated_at) VALUES(?,'hp','{}',10,'2026-09-14','2026-09-14')", (job,))
        connection.execute("INSERT INTO research_job_items(job_id,clinic_id) VALUES('both',?)", (existing,))
        connection.execute("INSERT INTO research_job_items(job_id,clinic_id,state,result,note) VALUES('both',?,'DONE','SUCCESS','調査済みの根拠')", (duplicate,))
        connection.execute("INSERT INTO research_job_items(job_id,clinic_id) VALUES('only_duplicate',?)", (duplicate,))
    assert store.reintegrate_existing()["merged_clinics"] == 1
    with store.connect() as connection:
        items = [tuple(row) for row in connection.execute("SELECT job_id,clinic_id,state,result,note FROM research_job_items ORDER BY job_id")]
        assert items == [("both", existing, "DONE", "SUCCESS", "調査済みの根拠"), ("only_duplicate", existing, "PENDING", "", "")]
        history = json.loads(connection.execute("SELECT before_json FROM change_history WHERE action='電話番号キーで再統合'").fetchone()[0])
        assert len(history["job_items"]) == 3
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()


def test_latest_official_snapshot_wins_and_raw_snapshots_remain(tmp_path, monkeypatch):
    store = ClinicStore(tmp_path / "snapshots.db")
    store.import_comdesk(comdesk())
    seed_separate(store, monkeypatch, [official()])
    updated = {**official(), "as_of": "2026-09-10", "manager_name": "新しい 院長"}
    duplicate = next(row["id"] for row in store.query(ALL) if not row["uuid"])
    with monkeypatch.context() as temporary:
        temporary.setattr("src.master.store.match_record", lambda *args, **kwargs: MasterMatch("MATCHED", [duplicate], "旧版で同じ医療機関番号を更新"))
        store.import_master([updated])
    result = store.reintegrate_existing()
    assert result["source_records"] == result["MATCHED"] == 1
    assert store.query(ALL)[0]["manager_name"] == "新しい 院長"
    assert store.query(ALL)[0]["source_as_of_date"] == "2026-09-10"
    with store.connect() as connection:
        sources = connection.execute("SELECT clinic_id,record_json FROM source_records WHERE source='厚生局' ORDER BY id").fetchall()
        assert len(sources) == 2 and sources[0]["clinic_id"] == sources[1]["clinic_id"]
        assert [json.loads(row["record_json"])["as_of"] for row in sources] == ["2026-09-01", "2026-09-10"]
    assert store.reintegrate_existing()["merged_clinics"] == 0


def test_running_research_lock_prevents_reintegration(tmp_path, monkeypatch):
    store = ClinicStore(tmp_path / "locked.db")
    store.import_comdesk(comdesk())
    seed_separate(store, monkeypatch, [official()])
    with FileLock(str(store.path) + ".research.lock", timeout=0):
        with pytest.raises(ValueError, match="一時停止"):
            store.reintegrate_existing()
    assert store.count(ALL) == 2


def test_failure_rolls_back_all_reintegration_changes(tmp_path, monkeypatch):
    store = ClinicStore(tmp_path / "rollback.db")
    store.import_comdesk(comdesk())
    seed_separate(store, monkeypatch, [official()])
    with store.connect() as connection:
        before = list(connection.iterdump())
    def fail(*args, **kwargs):
        raise RuntimeError("検証用の保存失敗")
    monkeypatch.setattr(store, "_project", fail)
    with pytest.raises(RuntimeError, match="保存失敗"):
        store.reintegrate_existing()
    with store.connect() as connection:
        assert list(connection.iterdump()) == before
