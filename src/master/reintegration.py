"""保存済み厚生局データをコムデスク案件へ再照合。元行と履歴を保持する。"""
from datetime import datetime
from pathlib import Path
import json

from filelock import FileLock, Timeout
from src.master.matching import MasterMatch, match_record, medical_key
from src.master.store import dumps, now
from src.normalizer.clinic_name import normalize_person
from src.normalizer.phone import tel_match_key


def has_comdesk(connection, cid):
    return connection.execute("SELECT 1 FROM comdesk_original_rows WHERE clinic_id=? LIMIT 1", (cid,)).fetchone() is not None


def merge_conflict(connection, source_id, target_id):
    source = connection.execute("SELECT * FROM clinics WHERE id=?", (source_id,)).fetchone()
    target = connection.execute("SELECT * FROM clinics WHERE id=?", (target_id,)).fetchone()
    if source["uuid"] and target["uuid"] and source["uuid"] != target["uuid"]:
        return "異なる既存UUID同士は統合できません。"
    source_values = {row["field"]: row["value_json"] for row in connection.execute("SELECT * FROM manual_overrides WHERE clinic_id=?", (source_id,))}
    for row in connection.execute("SELECT * FROM manual_overrides WHERE clinic_id=?", (target_id,)):
        if row["field"] in source_values and json.loads(source_values[row["field"]]) != json.loads(row["value_json"]):
            return "手動修正の値が異なります。両医院の修正内容を確認してください。"
    return ""


def merge_clinics(store, connection, source_id, target_id, reason):
    source_id = store._get(connection, source_id)["id"]
    target_id = store._get(connection, target_id)["id"]
    if source_id == target_id:
        return target_id
    problem = merge_conflict(connection, source_id, target_id)
    if problem:
        raise ValueError(problem)
    source = dict(connection.execute("SELECT * FROM clinics WHERE id=?", (source_id,)).fetchone())
    target = dict(connection.execute("SELECT * FROM clinics WHERE id=?", (target_id,)).fetchone())
    # 既存UUIDを持つ側の管理番号を残す。
    if source["uuid"] and not target["uuid"]:
        source_id, target_id = target_id, source_id
        source, target = target, source
    source_base, target_base = json.loads(source["base_json"]), json.loads(target["base_json"])
    if source["source_as_of_date"] > target["source_as_of_date"]:
        base = {**target_base, **source_base}
    else:
        base = {**source_base, **target_base}
    history_before = {"source": source, "target": target, "job_items": []}
    research_rows = [dict(row) for row in connection.execute("SELECT * FROM research_results WHERE clinic_id IN (?,?) ORDER BY updated_at,clinic_id", (source_id, target_id))]
    history_before["research_results"] = research_rows
    if research_rows:
        research = {}
        for row in research_rows:
            research.update(json.loads(row["result_json"]))
        previous_doctor = research.get("doctor_name")
        if previous_doctor and base.get("manager_name") and normalize_person(previous_doctor) != normalize_person(base["manager_name"]):
            for field in list(research):
                if field.startswith(("age_", "license_", "graduation_")) or field == "doctor_name":
                    research.pop(field)
            research.update(age_probability_under_59=None, age_estimation_confidence="REVIEW",
                            age_estimation_reason="再統合後の院長の経歴を確認してください。")
        connection.execute("INSERT OR REPLACE INTO research_results VALUES(?,?,?)", (target_id, dumps(research), max(row["updated_at"] for row in research_rows)))
    connection.execute("INSERT OR IGNORE INTO manual_overrides SELECT ?,field,value_json,source,note,updated_at FROM manual_overrides WHERE clinic_id=?", (target_id, source_id))
    connection.execute("INSERT OR IGNORE INTO hp_pages SELECT ?,url,page_json,checked_at FROM hp_pages WHERE clinic_id=?", (target_id, source_id))
    for item in connection.execute("SELECT * FROM research_job_items WHERE clinic_id=?", (source_id,)).fetchall():
        history_before["job_items"].append(dict(item))
        existing = connection.execute("SELECT * FROM research_job_items WHERE job_id=? AND clinic_id=?", (item["job_id"], target_id)).fetchone()
        if existing:
            history_before["job_items"].append(dict(existing))
            if item["state"] == "DONE" and existing["state"] != "DONE":
                connection.execute("UPDATE research_job_items SET state='DONE',result=?,note=?,lease_until='' WHERE job_id=? AND clinic_id=?", (item["result"], item["note"], item["job_id"], target_id))
            connection.execute("DELETE FROM research_job_items WHERE job_id=? AND clinic_id=?", (item["job_id"], source_id))
        else:
            connection.execute("UPDATE research_job_items SET clinic_id=? WHERE job_id=? AND clinic_id=?", (target_id, item["job_id"], source_id))
    connection.execute("UPDATE clinics SET merged_into=?,merge_hold=0 WHERE id=?", (target_id, source_id))
    connection.execute("UPDATE clinics SET uuid=?,base_json=?,first_seen_at=?,last_seen_at=?,source_as_of_date=?,merge_hold=0 WHERE id=?",
        (target["uuid"] or source["uuid"], dumps(base), min(target["first_seen_at"], source["first_seen_at"]),
         max(target["last_seen_at"], source["last_seen_at"]), max(target["source_as_of_date"], source["source_as_of_date"]), target_id))
    connection.execute("UPDATE source_records SET clinic_id=?,match_status='MATCHED',match_reason=? WHERE clinic_id=?", (target_id, reason, source_id))
    connection.execute("UPDATE comdesk_original_rows SET clinic_id=? WHERE clinic_id=?", (target_id, source_id))
    connection.execute("UPDATE match_reviews SET resolved_clinic_id=? WHERE resolved_clinic_id=?", (target_id, source_id))
    for review in connection.execute("SELECT id,candidates_json FROM match_reviews WHERE status='PENDING'").fetchall():
        candidates = json.loads(review["candidates_json"])
        mapped = sorted({target_id if cid == source_id else cid for cid in candidates})
        if mapped != candidates:
            connection.execute("UPDATE match_reviews SET candidates_json=? WHERE id=?", (dumps(mapped), review["id"]))
    store._project(connection, target_id)
    store._history(connection, target_id, "電話番号キーで再統合", history_before,
                   {"source_id": source_id, "target_id": target_id}, reason)
    return target_id


def mark_review(store, connection, source, cid, match):
    pending = connection.execute("SELECT id FROM match_reviews WHERE source_record_id=? AND status='PENDING'", (source["id"],)).fetchone()
    if pending:
        connection.execute("UPDATE match_reviews SET candidates_json=?,updated_at=? WHERE id=?", (dumps(match.candidates), now(), pending[0]))
    else:
        connection.execute("INSERT INTO match_reviews(source_record_id,candidates_json,updated_at) VALUES(?,?,?)", (source["id"], dumps(match.candidates), now()))
    connection.execute("UPDATE source_records SET match_status='AMBIGUOUS',match_reason=?,match_score=? WHERE id=?", (match.reason, match.score, source["id"]))
    if cid is not None:
        was_held = connection.execute("SELECT merge_hold FROM clinics WHERE id=?", (cid,)).fetchone()[0]
        connection.execute("UPDATE clinics SET merge_hold=1 WHERE id=?", (cid,))
        if not was_held:
            store._history(connection, cid, "再統合の確認待ち", {}, {"candidates": match.candidates}, match.reason)


def latest_sources(connection):
    latest = {}
    for row in connection.execute("SELECT * FROM source_records WHERE source='厚生局' ORDER BY id"):
        source = dict(row)
        record = json.loads(source["record_json"])
        key = medical_key(record) or ("clinic:" + str(source["clinic_id"]) if source["clinic_id"] is not None else "source:" + str(source["id"]))
        order = (record.get("as_of", ""), source["id"])
        if key not in latest or order > latest[key][0]:
            latest[key] = (order, source)
    return [item[1] for item in sorted(latest.values(), key=lambda item: item[1]["id"])]


def _reintegrate_locked(store):
    with store.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        sources = latest_sources(connection)
        if not sources:
            raise ValueError("保存済みの厚生局データがありません。先に厚生局データを取り込んでください。")
        if not connection.execute("SELECT 1 FROM comdesk_original_rows WHERE clinic_id IS NOT NULL LIMIT 1").fetchone():
            raise ValueError("既存コムデスク案件が登録されていません。コムデスクCSV・Excelを登録してから再統合してください。")
        backup_path = store.path.parent / "backups" / (store.path.stem + "_before_reintegration_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".sqlite3")
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        # 書込ロックを確保後、変更前の状態を別の読取接続からバックアップ。
        backup_path.write_bytes(store.backup_bytes())
        connection.execute("UPDATE research_jobs SET status='PAUSED' WHERE status='RUNNING'")
        connection.execute("UPDATE research_job_items SET state='PENDING' WHERE state='RUNNING'")
        connection.executemany("UPDATE clinics SET tel_match_key=? WHERE id=?", [(tel_match_key(row["phone"]), row["id"]) for row in connection.execute("SELECT id,phone FROM clinics")])
        merged_count = 0
        created_count = 0
        processed = set()
        for selected in sources:
            source = dict(connection.execute("SELECT * FROM source_records WHERE id=?", (selected["id"],)).fetchone())
            record = json.loads(source["record_json"])
            if source["match_reason"].startswith("手動確認:"):
                continue
            if source["clinic_id"] is None:
                match = match_record(connection, record)
                cid = store._upsert(connection, record, match, "厚生局", source["source_hash"], source["row_number"],
                                    new_flag=True, existing_source_id=source["id"])
                created_count += int(match.status == "NEW")
                if cid is not None:
                    processed.add(cid)
                continue
            cid = store._get(connection, source["clinic_id"])["id"]
            if cid in processed or has_comdesk(connection, cid):
                continue
            processed.add(cid)
            current = dict(connection.execute("SELECT * FROM clinics WHERE id=?", (cid,)).fetchone())
            match = match_record(connection, json.loads(current["base_json"]), exclude_ids=(cid,), comdesk_only=True, track_identity=False)
            if match.status == "MATCHED":
                problem = merge_conflict(connection, cid, match.candidates[0])
                if problem:
                    mark_review(store, connection, source, cid, MasterMatch("AMBIGUOUS", match.candidates, problem))
                    continue
                target_id = merge_clinics(store, connection, cid, match.candidates[0], match.reason)
                connection.execute("UPDATE match_reviews SET status='RESOLVED',resolved_clinic_id=?,note=?,updated_at=? WHERE source_record_id=? AND status='PENDING'", (target_id, "新しい電話番号キーで再照合", now(), source["id"]))
                merged_count += 1
            elif match.status == "AMBIGUOUS":
                mark_review(store, connection, source, cid, match)
            else:
                connection.execute("UPDATE clinics SET merge_hold=0 WHERE id=?", (cid,))
                connection.execute("UPDATE source_records SET match_status='NEW',match_reason=? WHERE id=?", (match.reason, source["id"]))
                connection.execute("UPDATE match_reviews SET status='RESOLVED',resolved_clinic_id=?,note=?,updated_at=? WHERE source_record_id=? AND status='PENDING'", (cid, "再照合で対応する既存案件なし", now(), source["id"]))
        counts = {"MATCHED": 0, "NEW": 0, "AMBIGUOUS": 0}
        for selected in sources:
            source = connection.execute("SELECT * FROM source_records WHERE id=?", (selected["id"],)).fetchone()
            if source["clinic_id"] is None:
                counts["AMBIGUOUS"] += 1
            else:
                cid = store._get(connection, source["clinic_id"])["id"]
                held = connection.execute("SELECT merge_hold FROM clinics WHERE id=?", (cid,)).fetchone()[0]
                counts["AMBIGUOUS" if held else "MATCHED" if has_comdesk(connection, cid) else "NEW"] += 1
        result = {**counts, "reintegrated": True, "merged_clinics": merged_count, "created_clinics": created_count,
                  "source_records": len(sources), "backup_path": str(backup_path)}
        store._history(connection, None, "再統合完了", {}, result)
        return result


def reintegrate_existing(store):
    try:
        with FileLock(str(store.path) + ".research.lock", timeout=0):
            return _reintegrate_locked(store)
    except Timeout as error:
        raise ValueError("自動調査が実行中です。一時停止してから再統合してください。") from error
