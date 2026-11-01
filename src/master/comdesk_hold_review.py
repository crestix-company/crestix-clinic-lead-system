"""Isolated Comdesk HOLD review store and revision-checked CSV exchange.

This module deliberately does not import a Clinic/Supabase repository. Review
decisions are evidence records only; they never write to clinic master data.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import sqlite3
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path

DECISIONS = {
    "同一医院と確認": "SAME_CLINIC",
    "別医院と確認": "DIFFERENT_CLINIC",
    "追加調査が必要": "NEEDS_RESEARCH",
    "保留": "HOLD",
}
DECISION_LABELS = {value: key for key, value in DECISIONS.items()}
CSV_FIELDS = (
    "review_id", "revision", "source_batch_id", "source_row_id", "source_row_hash", "review_basis_hash",
    "comdesk_uuid", "comdesk_name", "comdesk_phone", "comdesk_address",
    "comdesk_prefecture", "candidate_clinic_id", "clinic_uuid", "clinic_medical_key",
    "clinic_name", "clinic_phone", "clinic_prefecture", "clinic_address", "clinic_merged_into",
    "clinic_hp_status", "clinic_official_hp_url", "clinic_maps_status", "clinic_maps_url",
    "clinic_snapshot_at",
    "hold_reason_code", "matched_fields", "mismatched_fields", "candidate_count",
    "review_status", "reviewer_decision", "reviewer_id", "reviewed_at", "review_note",
)
IMMUTABLE_CSV_FIELDS = (
    "source_batch_id", "source_row_id", "source_row_hash", "review_basis_hash", "candidate_clinic_id",
    "comdesk_uuid", "comdesk_name", "comdesk_phone", "comdesk_address",
    "comdesk_prefecture", "clinic_uuid", "clinic_medical_key", "clinic_name", "clinic_phone",
    "clinic_prefecture", "clinic_address", "clinic_merged_into", "clinic_hp_status",
    "clinic_official_hp_url", "clinic_maps_status", "clinic_maps_url", "clinic_snapshot_at", "hold_reason_code",
    "matched_fields", "mismatched_fields", "candidate_count",
)


def _sha(*parts: object) -> str:
    return hashlib.sha256("\x1f".join("" if x is None else str(x) for x in parts).encode("utf-8")).hexdigest()


def _csv_safe(value: object) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def default_review_db_path() -> Path:
    configured = os.environ.get("CLINIC_COMDESK_REVIEW_DB", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "Library" / "Application Support" / "Crestix" / "Clinic Lead System" / "comdesk_hold_review.sqlite3"


class HoldReviewStore:
    """Local isolated SQLite ledger. Each candidate is independently reviewable."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        parent_existed = self.path.parent.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not parent_existed:
            os.chmod(self.path.parent, 0o700)
        elif stat.S_IMODE(self.path.parent.stat().st_mode) & 0o077:
            raise PermissionError("review ledger directory must not be accessible by group/other")
        self._init()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init(self):
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS review_cases (
                    review_id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL DEFAULT 1,
                    source_batch_id TEXT NOT NULL,
                    source_row_id TEXT NOT NULL,
                    source_row_hash TEXT NOT NULL,
                    review_basis_hash TEXT NOT NULL DEFAULT '',
                    candidate_clinic_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    review_status TEXT NOT NULL DEFAULT 'UNREVIEWED',
                    reviewer_decision TEXT NOT NULL DEFAULT '',
                    reviewer_id TEXT NOT NULL DEFAULT '',
                    reviewed_at TEXT NOT NULL DEFAULT '',
                    review_note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source_batch_id, source_row_id, candidate_clinic_id)
                );
                CREATE TABLE IF NOT EXISTS review_events (
                    event_id TEXT PRIMARY KEY,
                    review_id TEXT NOT NULL REFERENCES review_cases(review_id),
                    revision INTEGER NOT NULL,
                    review_status TEXT NOT NULL,
                    reviewer_decision TEXT NOT NULL,
                    reviewer_id TEXT NOT NULL,
                    reviewed_at TEXT NOT NULL,
                    review_note TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS review_cases_status_idx ON review_cases(review_status);
                CREATE INDEX IF NOT EXISTS review_cases_source_idx ON review_cases(source_batch_id, source_row_id);
            """)
            columns={r[1] for r in db.execute("PRAGMA table_info(review_cases)")}
            if "review_basis_hash" not in columns:
                db.execute("ALTER TABLE review_cases ADD COLUMN review_basis_hash TEXT NOT NULL DEFAULT ''")
        os.chmod(self.path, 0o600)

    @staticmethod
    def make_case(payload: dict) -> dict:
        source_batch_id = str(payload.get("source_batch_id") or payload.get("source_hash") or "").strip()
        source_row_id = str(payload.get("source_row_id") or payload.get("source_row") or "").strip()
        candidate_id = str(payload.get("candidate_clinic_id") or "").strip()
        if not source_batch_id or not source_row_id or not candidate_id:
            raise ValueError("source_batch_id, source_row_id, candidate_clinic_id are required")
        source_row_hash = str(payload.get("source_row_hash") or _sha(
            source_batch_id, source_row_id, payload.get("source_clinic_identity_hash"), payload.get("comdesk_uuid")
        ))
        review_basis_hash = str(payload.get("review_basis_hash") or _sha(
            candidate_id, payload.get("clinic_uuid"), payload.get("clinic_name"), payload.get("clinic_phone"),
            payload.get("clinic_address"), payload.get("clinic_medical_key"), payload.get("candidate_evidence"),
            payload.get("merge_hold"), payload.get("clinic_merged_into"),
            payload.get("evidence_rule_version", "comdesk-hold-review-v1")
        ))
        review_id = str(payload.get("review_id") or _sha(source_batch_id, source_row_id, candidate_id))
        data = dict(payload)
        data.update(source_batch_id=source_batch_id, source_row_id=source_row_id,
                    source_row_hash=source_row_hash, review_basis_hash=review_basis_hash,
                    candidate_clinic_id=candidate_id)
        return {"review_id": review_id, "source_batch_id": source_batch_id,
                "source_row_id": source_row_id, "source_row_hash": source_row_hash,
                "review_basis_hash": review_basis_hash,
                "candidate_clinic_id": candidate_id, "payload_json": json.dumps(data, ensure_ascii=False, sort_keys=True)}

    def seed(self, cases: list[dict]) -> int:
        now = _now()
        prepared = [self.make_case(row) for row in cases]
        if len({r["review_id"] for r in prepared}) != len(prepared):
            raise ValueError("duplicate review case in seed")
        inserted = 0
        with self._connect() as db:
            for row in prepared:
                cur = db.execute("""INSERT OR IGNORE INTO review_cases
                    (review_id,source_batch_id,source_row_id,source_row_hash,review_basis_hash,candidate_clinic_id,payload_json,created_at,updated_at)
                    VALUES (:review_id,:source_batch_id,:source_row_id,:source_row_hash,:review_basis_hash,:candidate_clinic_id,:payload_json,:now,:now)""",
                    {**row, "now": now})
                inserted += cur.rowcount
                existing = db.execute("SELECT source_row_hash,review_basis_hash,review_status,revision FROM review_cases WHERE review_id=?", (row["review_id"],)).fetchone()
                if existing and existing[0] != row["source_row_hash"]:
                    raise ValueError("review_id collision with different source row hash")
                if existing and not existing[1]:
                    db.execute("UPDATE review_cases SET review_basis_hash=?,payload_json=?,updated_at=? WHERE review_id=?",
                               (row["review_basis_hash"],row["payload_json"],now,row["review_id"]))
                elif existing and existing[1] != row["review_basis_hash"] and existing[2] == "UNREVIEWED":
                    db.execute("UPDATE review_cases SET review_basis_hash=?,payload_json=?,updated_at=? WHERE review_id=?",
                               (row["review_basis_hash"],row["payload_json"],now,row["review_id"]))
                elif existing and existing[1] != row["review_basis_hash"]:
                    revision=int(existing[3])+1
                    db.execute("UPDATE review_cases SET revision=?,review_basis_hash=?,payload_json=?,review_status='UNREVIEWED',reviewer_decision='',reviewer_id='',reviewed_at='',review_note='',updated_at=? WHERE review_id=?",
                               (revision,row["review_basis_hash"],row["payload_json"],now,row["review_id"]))
                    db.execute("INSERT INTO review_events VALUES (?,?,?,?,?,?,?,?)",
                               (str(uuid.uuid4()),row["review_id"],revision,"UNREVIEWED","","SYSTEM_REVALIDATION",now,
                                "Clinic候補情報または照合根拠が変化したため、過去判定を無効化して再レビュー対象に戻した。"))
                elif existing and existing[2] == "UNREVIEWED" and int(existing[3]) == 1:
                    db.execute("UPDATE review_cases SET payload_json=?,updated_at=? WHERE review_id=?",
                               (row["payload_json"],now,row["review_id"]))
        return inserted

    def rows(self, *, status: str | None = None, reason: str | None = None,
             source: str | None = None, uuid_conflict: str | None = None,
             difference: str | None = None, keyword: str = "") -> list[dict]:
        clauses=[]; params=[]
        if status and status != "すべて": clauses.append("review_status=?"); params.append(status)
        if reason and reason != "すべて": clauses.append("json_extract(payload_json,'$.hold_reason_code')=?"); params.append(reason)
        if source and source != "すべて": clauses.append("json_extract(payload_json,'$.source_file')=?"); params.append(source)
        if uuid_conflict in ("あり", "なし"):
            clauses.append("json_extract(payload_json,'$.uuid_conflict')=?"); params.append("あり" if uuid_conflict=="あり" else "なし")
        if difference and difference != "すべて":
            clauses.append("json_extract(payload_json,'$.address_difference_class')=?"); params.append(difference)
        if keyword:
            clauses.append("(payload_json LIKE ? OR candidate_clinic_id LIKE ? OR source_row_id LIKE ?)")
            params += [f"%{keyword}%"]*3
        sql="SELECT * FROM review_cases"+(" WHERE "+" AND ".join(clauses) if clauses else "")+" ORDER BY source_batch_id, source_row_id, candidate_clinic_id"
        with self._connect() as db:
            out=[]
            for r in db.execute(sql,params):
                item=dict(r); item.update(json.loads(item.pop("payload_json"))); out.append(item)
            return out

    def summary(self) -> dict:
        with self._connect() as db:
            total=db.execute("SELECT count(*) FROM review_cases").fetchone()[0]
            statuses={r[0]:r[1] for r in db.execute("SELECT review_status,count(*) FROM review_cases GROUP BY review_status")}
            uuids=db.execute("SELECT count(*) FROM review_cases WHERE json_extract(payload_json,'$.uuid_conflict')='あり'").fetchone()[0]
            identities=db.execute("SELECT count(DISTINCT source_batch_id||':'||source_row_id) FROM review_cases").fetchone()[0]
            clinics=db.execute("SELECT count(DISTINCT json_extract(payload_json,'$.source_clinic_identity_hash')) FROM review_cases WHERE coalesce(json_extract(payload_json,'$.source_clinic_identity_hash'),'')<>''").fetchone()[0]
        return {"total_candidate_rows":total,"total_input_rows":identities,"unique_input_clinics":clinics,"statuses":statuses,"uuid_conflicts":uuids}

    def decide(self, review_id: str, *, expected_revision: int, decision: str,
               reviewer_id: str, note: str = "", reviewed_at: str | None = None) -> dict:
        if decision not in DECISIONS.values(): raise ValueError("unsupported reviewer decision")
        reviewer_id=reviewer_id.strip(); note=note.strip()
        if not reviewer_id: raise ValueError("reviewer_id is required")
        if decision == "SAME_CLINIC" and not note: raise ValueError("same-clinic decision requires evidence note")
        reviewed_at=reviewed_at or _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row=db.execute("SELECT revision,source_batch_id,source_row_id,candidate_clinic_id FROM review_cases WHERE review_id=?",(review_id,)).fetchone()
            if row is None: raise ValueError("unknown review_id")
            if int(row[0]) != int(expected_revision): raise ValueError("stale revision")
            if decision == "SAME_CLINIC":
                other=db.execute("SELECT 1 FROM review_cases WHERE source_batch_id=? AND source_row_id=? AND candidate_clinic_id<>? AND review_status='SAME_CLINIC' LIMIT 1",
                                 (row[1],row[2],str(row[3]))).fetchone()
                if other: raise ValueError("another Clinic candidate is already confirmed as the same clinic")
            revision=int(row[0])+1
            db.execute("UPDATE review_cases SET revision=?,review_status=?,reviewer_decision=?,reviewer_id=?,reviewed_at=?,review_note=?,updated_at=? WHERE review_id=?",
                       (revision,decision,decision,reviewer_id,reviewed_at,note,reviewed_at,review_id))
            db.execute("INSERT INTO review_events VALUES (?,?,?,?,?,?,?,?)",
                       (str(uuid.uuid4()),review_id,revision,decision,decision,reviewer_id,reviewed_at,note))
        return {"review_id":review_id,"revision":revision,"review_status":decision,"reviewer_decision":decision,"reviewer_id":reviewer_id,"reviewed_at":reviewed_at,"review_note":note}

    def history(self, review_id: str) -> list[dict]:
        with self._connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM review_events WHERE review_id=? ORDER BY revision",(review_id,))]

    def prior_reviews(self, *, source_row_hash: str, candidate_clinic_id: str,
                      review_basis_hash: str) -> list[dict]:
        """Return exact-basis prior decisions as suggestions, never auto-apply them."""
        with self._connect() as db:
            return [dict(r) for r in db.execute("""SELECT review_id,source_batch_id,source_row_id,
                revision,review_status,reviewer_id,reviewed_at,review_note
                FROM review_cases WHERE source_row_hash=? AND candidate_clinic_id=?
                AND review_basis_hash=? AND review_status<>'UNREVIEWED'
                ORDER BY reviewed_at DESC""", (source_row_hash, str(candidate_clinic_id), review_basis_hash))]

    def export_csv(self, *, rows: list[dict] | None = None) -> bytes:
        rows=self.rows() if rows is None else rows
        s=io.StringIO(newline="")
        w=csv.DictWriter(s,fieldnames=CSV_FIELDS,extrasaction="ignore",quoting=csv.QUOTE_ALL)
        w.writeheader()
        for r in rows:
            p=r.get("payload_json")
            payload=json.loads(p) if isinstance(p,str) else {}
            merged={**payload,**r}
            merged["reviewer_decision"] = DECISION_LABELS.get(merged.get("reviewer_decision", ""), merged.get("reviewer_decision", ""))
            w.writerow({k:_csv_safe(merged.get(k,"")) for k in CSV_FIELDS})
        return b"\xef\xbb\xbf"+s.getvalue().encode("utf-8")

    def preview_csv(self, data: bytes, *, reviewer_default: str = "") -> dict:
        text=data.decode("utf-8-sig")
        reader=csv.DictReader(io.StringIO(text,newline=""))
        required=set(CSV_FIELDS)
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            return {"valid":False,"errors":["必須列が不足しています"],"accepted":[]}
        errors=[]; accepted=[]; already_applied=[]; seen=set(); same_by_input={}
        current={r["review_id"]:r for r in self.rows()}
        for line,row in enumerate(reader,2):
            rid=row["review_id"].lstrip("'"); key=rid
            if key in seen: errors.append(f"{line}行: review_id重複"); continue
            seen.add(key)
            case=current.get(key)
            if not case: errors.append(f"{line}行: review_id不明"); continue
            def clean(k): return (row.get(k) or "").lstrip("'").strip()
            fields=("revision",*IMMUTABLE_CSV_FIELDS)
            expected={"revision":str(case["revision"]),"source_batch_id":case["source_batch_id"],"source_row_id":case["source_row_id"],"source_row_hash":case["source_row_hash"],"review_basis_hash":case["review_basis_hash"],"candidate_clinic_id":str(case["candidate_clinic_id"])}
            expected.update({field:str(case.get(field,"")) for field in IMMUTABLE_CSV_FIELDS})
            bad=[k for k in fields if clean(k)!=expected[k]]
            if bad: errors.append(f"{line}行: 識別情報不一致({','.join(bad)})"); continue
            label=clean("reviewer_decision")
            decision=DECISIONS.get(label, label if label in DECISION_LABELS else "")
            if not decision: errors.append(f"{line}行: 判定値が不正"); continue
            reviewer=clean("reviewer_id") or reviewer_default.strip()
            if not reviewer: errors.append(f"{line}行: 確認者が未入力"); continue
            note=clean("review_note")
            if decision=="SAME_CLINIC" and not note: errors.append(f"{line}行: 同一医院判定に根拠メモが必要"); continue
            if case["review_status"] not in ("UNREVIEWED", decision): errors.append(f"{line}行: 既存レビューと競合"); continue
            input_key=(case["source_batch_id"],case["source_row_id"])
            if decision == "SAME_CLINIC":
                prior_same=[c for c in current.values() if c["source_batch_id"]==input_key[0] and c["source_row_id"]==input_key[1]
                            and c["review_status"]=="SAME_CLINIC" and c["review_id"]!=key]
                planned_same=same_by_input.get(input_key)
                if prior_same or (planned_same and planned_same!=str(case["candidate_clinic_id"])):
                    errors.append(f"{line}行: 同一入力に複数のClinic候補を同一医院として指定しています"); continue
                same_by_input[input_key]=str(case["candidate_clinic_id"])
            if (case["review_status"] == decision and case.get("reviewer_id", "") == reviewer
                    and case.get("review_note", "") == note):
                already_applied.append(key)
                continue
            accepted.append({"review_id":key,"revision":int(expected["revision"]),"decision":decision,"reviewer_id":reviewer,"review_note":note,"reviewed_at":clean("reviewed_at") or _now()})
        return {"valid":not errors,"errors":errors,"accepted":accepted,"already_applied":already_applied}

    def apply_preview(self, preview: dict) -> int:
        if not preview.get("valid"): raise ValueError("CSV preview contains validation errors")
        reviewed=[]
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # Validate the whole batch again while holding the write lock. A
            # stale member aborts the entire import instead of partial apply.
            accepted_by_id={r["review_id"]:r for r in preview["accepted"]}
            cases_by_id={}
            for row in preview["accepted"]:
                current=db.execute("SELECT revision,source_batch_id,source_row_id,candidate_clinic_id FROM review_cases WHERE review_id=?",(row["review_id"],)).fetchone()
                if current is None or int(current[0]) != int(row["revision"]):
                    raise ValueError("stale revision; no CSV decisions were applied")
                cases_by_id[row["review_id"]]=current
                reviewed.append(row)
            planned_same={}
            for row in reviewed:
                if row["decision"] != "SAME_CLINIC": continue
                current=cases_by_id[row["review_id"]]
                input_key=(current[1],current[2]); cid=str(current[3])
                if input_key in planned_same and planned_same[input_key] != cid:
                    raise ValueError("multiple Clinic candidates marked SAME_CLINIC; no CSV decisions were applied")
                planned_same[input_key]=cid
                siblings=db.execute("SELECT review_id,candidate_clinic_id,review_status FROM review_cases WHERE source_batch_id=? AND source_row_id=? AND candidate_clinic_id<>?",
                                    (input_key[0],input_key[1],cid)).fetchall()
                for sibling in siblings:
                    planned=accepted_by_id.get(sibling[0])
                    final_status=planned["decision"] if planned else sibling[2]
                    if final_status=="SAME_CLINIC":
                        raise ValueError("another Clinic candidate is already confirmed as the same clinic; no CSV decisions were applied")
            for row in reviewed:
                revision=int(row["revision"])+1
                db.execute("UPDATE review_cases SET revision=?,review_status=?,reviewer_decision=?,reviewer_id=?,reviewed_at=?,review_note=?,updated_at=? WHERE review_id=?",
                           (revision,row["decision"],row["decision"],row["reviewer_id"],row["reviewed_at"],row["review_note"],row["reviewed_at"],row["review_id"]))
                db.execute("INSERT INTO review_events VALUES (?,?,?,?,?,?,?,?)",
                           (str(uuid.uuid4()),row["review_id"],revision,row["decision"],row["decision"],row["reviewer_id"],row["reviewed_at"],row["review_note"]))
        return len(reviewed)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def cases_from_reports(report_dir: str | Path, csv_dir: str | Path,
                       candidate_snapshot_path: str | Path | None = None) -> list[dict]:
    """Build isolated review cases from the latest read-only reconciliation artifacts."""
    from src.master.comdesk_reconciliation import discover_comdesk_csvs, input_rows
    report_dir=Path(report_dir); sources,_=discover_comdesk_csvs(csv_dir); items=input_rows(sources)
    by_key={(i["source_hash"],str(i["source_row"])):i for i in items}
    with (report_dir/"hold_analysis.csv").open(encoding="utf-8-sig",newline="") as f: holds=list(csv.DictReader(f))
    with (report_dir/"address_difference_review.csv").open(encoding="utf-8-sig",newline="") as f: diffs=list(csv.DictReader(f))
    diff_by={(r["source_file"],r["source_row"]):r for r in diffs}
    snapshot={}
    if candidate_snapshot_path:
        with Path(candidate_snapshot_path).open(encoding="utf-8") as f: snapshot=json.load(f)
    snapshot_at=(snapshot.get("_meta") or {}).get("captured_at", "")
    cases=[]
    for hold in holds:
        item=by_key.get((hold["source_hash"],hold["source_row"]))
        if not item: raise ValueError("HOLD source row not found in source CSVs")
        rec=item["record"]
        candidates=[x for x in hold["candidate_clinic_ids"].split(";") if x]
        evidence={str(x["clinic_id"]):x for x in json.loads(hold.get("candidate_evidence") or "[]")}
        for cid in candidates:
            ev=evidence.get(cid,{})
            clinic=snapshot.get(cid,{})
            matched=[]; mismatched=[]
            if ev.get("phone_exact"): matched.append("電話番号")
            elif rec.get("phone") and clinic.get("clinic_phone"): mismatched.append("電話番号")
            if ev.get("name_exact_loose"): matched.append("医院名（正規化）")
            elif rec.get("clinic_name") and clinic.get("clinic_name"): mismatched.append("医院名")
            if ev.get("address_exact_normalized") or ev.get("address_exact_safe_key"): matched.append("住所（正規化）")
            elif rec.get("address") and clinic.get("clinic_address"): mismatched.append("住所")
            uuid_conflict=bool(rec.get("uuid") and clinic.get("clinic_uuid") and rec.get("uuid")!=clinic.get("clinic_uuid")) or ev.get("uuid_state")=="UUID_CONFLICT"
            if ev.get("uuid_state")=="UUID_MATCH": matched.append("Comdesk UUID")
            if uuid_conflict: mismatched.append("Comdesk UUID / Clinic UUID")
            cases.append({"source_batch_id":hold["source_hash"],"source_row_id":hold["source_row"],
                "source_row_hash":item["row_fingerprint"],
                "source_clinic_identity_hash":hold.get("source_clinic_identity_hash",""),
                "source_file":item["source_file"],"project_scope":item.get("project_scope",""),
                "project_scope_source":item.get("project_scope_source",""),"comdesk_uuid":rec.get("uuid",""),
                "comdesk_name":rec.get("clinic_name",""),"comdesk_phone":rec.get("phone",""),
                "comdesk_address":rec.get("address",""),"comdesk_url_candidate":rec.get("hp_candidate_url",""),
                "comdesk_prefecture":rec.get("prefecture",""),
                "comdesk_case_id":rec.get("comdesk_case_id",""),"candidate_clinic_id":cid,
                "candidate_evidence":ev,
                "clinic_uuid":clinic.get("clinic_uuid",""),"clinic_medical_key":clinic.get("medical_institution_number") or clinic.get("medical_key",""),
                "clinic_name":clinic.get("clinic_name",""),"clinic_phone":clinic.get("clinic_phone",""),
                "clinic_prefecture":clinic.get("clinic_prefecture",""),"clinic_address":clinic.get("clinic_address",""),"clinic_merged_into":clinic.get("merged_into"),
                "clinic_hp_status":clinic.get("hp_status",""),"clinic_official_hp_url":clinic.get("official_hp_url",""),
                "clinic_maps_status":clinic.get("maps_status",""),"clinic_maps_url":clinic.get("maps_url",""),
                "clinic_snapshot_at":snapshot_at,
                "merge_hold":clinic.get("merge_hold",ev.get("merge_hold",False)),
                "hold_reason_code":hold.get("reason",""),"candidate_count":len(candidates),
                "matched_fields":"、".join(matched),"mismatched_fields":"、".join(mismatched),"name_similarity":ev.get("name_similarity",""),
                "address_similarity":ev.get("address_similarity",""),"phone_candidate_count":ev.get("phone_candidate_count",""),
                "uuid_conflict":"あり" if uuid_conflict else "なし",
                "address_difference_class":diff_by.get((item["source_file"],str(item["source_row"])),{}).get("address_difference_class","")})
    return cases
