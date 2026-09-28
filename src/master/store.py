"""SQLite営業DB。元行・自動値・手動値を分離し、全変更をトランザクション化。"""
from contextlib import contextmanager
from datetime import datetime, date, timezone
from pathlib import Path
from io import BytesIO
import hashlib
import json
import sqlite3
import tempfile
import os
import uuid as uuidlib
import pandas as pd
from src.master.comdesk import infer_comdesk_columns, record_from_row, new_export_row
from src.io.output_writer import csv_bytes, xlsx_bytes
from src.master.matching import match_record, medical_key, MasterMatch
from src.master.filters import Filters, where, clauses
from src.normalizer.phone import normalize_phone, tel_match_key
from src.normalizer.address import normalize_address
from src.normalizer.clinic_name import normalize_clinic_name, normalize_person, person_from_owner
from src.normalizer.departments import normalize_departments
from src.utils.date_utils import parse_date, today_japan


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS clinics(
 id INTEGER PRIMARY KEY, uuid TEXT NOT NULL DEFAULT '', clinic_name TEXT NOT NULL DEFAULT '',
 phone TEXT NOT NULL DEFAULT '', address TEXT NOT NULL DEFAULT '', phone_norm TEXT NOT NULL DEFAULT '',
 tel_match_key TEXT NOT NULL DEFAULT '', merge_hold INTEGER NOT NULL DEFAULT 0,
 name_norm TEXT NOT NULL DEFAULT '', name_prefix TEXT NOT NULL DEFAULT '', address_norm TEXT NOT NULL DEFAULT '',
 medical_key TEXT NOT NULL DEFAULT '', prefecture TEXT NOT NULL DEFAULT '', medical_type TEXT NOT NULL DEFAULT '',
 base_json TEXT NOT NULL DEFAULT '{}', effective_json TEXT NOT NULL DEFAULT '{}',
 active INTEGER NOT NULL DEFAULT 0, designation_date TEXT NOT NULL DEFAULT '', recent_until TEXT NOT NULL DEFAULT '',
 registration_reason TEXT NOT NULL DEFAULT '', owner_equal INTEGER, age_probability REAL,
 hp_status TEXT NOT NULL DEFAULT 'UNRESEARCHED', hp_url TEXT NOT NULL DEFAULT '', hp_rank TEXT NOT NULL DEFAULT 'UNKNOWN',
 signal_count INTEGER NOT NULL DEFAULT 0, hot_status TEXT NOT NULL DEFAULT '通常',
 departments_json TEXT NOT NULL DEFAULT '[]', treatments_json TEXT NOT NULL DEFAULT '[]', signals_json TEXT NOT NULL DEFAULT '[]',
 maps_presence_status TEXT NOT NULL DEFAULT '', maps_profile_url TEXT NOT NULL DEFAULT '', maps_website_url TEXT NOT NULL DEFAULT '',
 maps_match_method TEXT NOT NULL DEFAULT '', maps_checked_at TEXT NOT NULL DEFAULT '', exclude_reason TEXT NOT NULL DEFAULT '',
 first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, source_as_of_date TEXT NOT NULL DEFAULT '',
 is_new INTEGER NOT NULL DEFAULT 0, merged_into INTEGER REFERENCES clinics(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_clinic_uuid ON clinics(uuid) WHERE uuid<>'' AND merged_into IS NULL;
CREATE INDEX IF NOT EXISTS idx_clinic_med ON clinics(medical_key);
CREATE INDEX IF NOT EXISTS idx_clinic_phone ON clinics(phone_norm);
CREATE INDEX IF NOT EXISTS idx_clinic_tel_match ON clinics(tel_match_key);
CREATE INDEX IF NOT EXISTS idx_clinic_name_address ON clinics(name_norm,address_norm);
CREATE INDEX IF NOT EXISTS idx_clinic_prefix ON clinics(name_prefix,prefecture);
CREATE INDEX IF NOT EXISTS idx_clinic_filter ON clinics(hp_status,prefecture,hp_rank,signal_count);
CREATE INDEX IF NOT EXISTS idx_clinic_hot ON clinics(hot_status);
CREATE TABLE IF NOT EXISTS templates(id TEXT PRIMARY KEY, headers_json TEXT NOT NULL, mapping_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS comdesk_original_rows(
 id INTEGER PRIMARY KEY, clinic_id INTEGER REFERENCES clinics(id), template_id TEXT NOT NULL REFERENCES templates(id),
 row_json TEXT NOT NULL, uuid TEXT NOT NULL DEFAULT '', source_hash TEXT NOT NULL, row_number INTEGER NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(source_hash,row_number));
CREATE INDEX IF NOT EXISTS idx_original_clinic ON comdesk_original_rows(clinic_id);
CREATE TABLE IF NOT EXISTS source_records(
 id INTEGER PRIMARY KEY, clinic_id INTEGER REFERENCES clinics(id), source TEXT NOT NULL,
 record_json TEXT NOT NULL, source_hash TEXT NOT NULL, row_number INTEGER NOT NULL,
 match_status TEXT NOT NULL, match_reason TEXT NOT NULL, match_score REAL NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(source_hash,row_number));
CREATE TABLE IF NOT EXISTS import_batches(id TEXT PRIMARY KEY, source TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS research_results(
 clinic_id INTEGER PRIMARY KEY REFERENCES clinics(id), result_json TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS hp_pages(
 clinic_id INTEGER NOT NULL REFERENCES clinics(id), url TEXT NOT NULL, page_json TEXT NOT NULL, checked_at TEXT NOT NULL,
 PRIMARY KEY(clinic_id,url));
CREATE TABLE IF NOT EXISTS manual_overrides(
 clinic_id INTEGER NOT NULL REFERENCES clinics(id), field TEXT NOT NULL, value_json TEXT NOT NULL,
 source TEXT NOT NULL, note TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(clinic_id,field));
CREATE TABLE IF NOT EXISTS match_reviews(
 id INTEGER PRIMARY KEY, source_record_id INTEGER NOT NULL REFERENCES source_records(id), candidates_json TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'PENDING', resolved_clinic_id INTEGER REFERENCES clinics(id), note TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS change_history(
 id INTEGER PRIMARY KEY, clinic_id INTEGER, action TEXT NOT NULL, before_json TEXT NOT NULL,
 after_json TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS research_jobs(
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, options_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PAUSED',
 max_searches INTEGER NOT NULL, search_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS research_job_items(
 job_id TEXT NOT NULL REFERENCES research_jobs(id), clinic_id INTEGER NOT NULL REFERENCES clinics(id),
 state TEXT NOT NULL DEFAULT 'PENDING', result TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
 lease_until TEXT NOT NULL DEFAULT '', PRIMARY KEY(job_id,clinic_id));
CREATE TABLE IF NOT EXISTS search_cache(query_key TEXT PRIMARY KEY, query TEXT NOT NULL, result_json TEXT NOT NULL, searched_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS search_usage(
 id INTEGER PRIMARY KEY, month TEXT NOT NULL, job_id TEXT, query_key TEXT NOT NULL, attempted_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_usage_month ON search_usage(month);

CREATE TABLE IF NOT EXISTS google_maps_results(
 id INTEGER PRIMARY KEY, clinic_id INTEGER REFERENCES clinics(id), batch_id TEXT NOT NULL, row_number INTEGER NOT NULL,
 result_json TEXT NOT NULL, maps_match_status TEXT NOT NULL DEFAULT '', maps_match_method TEXT NOT NULL DEFAULT '',
 maps_profile_url TEXT NOT NULL DEFAULT '', maps_website_url TEXT NOT NULL DEFAULT '', scraped_at TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
 UNIQUE(batch_id,row_number));
CREATE INDEX IF NOT EXISTS idx_maps_clinic ON google_maps_results(clinic_id);
"""


class ClinicStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > 4:
                raise ValueError("このDBは新しいバージョンで作られています。対応するアプリで開いてください。")
            if "clinics" in tables:
                columns = {r[1] for r in conn.execute("PRAGMA table_info(clinics)")}
                if not {"base_json", "effective_json", "merged_into", "name_prefix"}.issubset(columns):
                    raise ValueError("既存DBの形式が異なります。DBを変更せず停止しました。別名のDBでV2を起動し、元CSVを取り込んでください。")
                if version < 4:
                    if version < 2:
                        suffix = ".before-v2.bak"
                    elif version < 3:
                        suffix = ".before-tel-key-v3.bak"
                    else:
                        suffix = ".before-maps-v4.bak"
                    backup_path = Path(str(self.path) + suffix)
                    if backup_path.exists():
                        backup_path = Path(str(backup_path) + "." + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
                    backup = sqlite3.connect(backup_path)
                    try:
                        conn.backup(backup)
                    finally:
                        backup.close()
                if "tel_match_key" not in columns:
                    conn.execute("ALTER TABLE clinics ADD COLUMN tel_match_key TEXT NOT NULL DEFAULT ''")
                if "merge_hold" not in columns:
                    conn.execute("ALTER TABLE clinics ADD COLUMN merge_hold INTEGER NOT NULL DEFAULT 0")
                for col in ("maps_presence_status","maps_profile_url","maps_website_url","maps_match_method","maps_checked_at","exclude_reason"):
                    if col not in columns:
                        conn.execute(f"ALTER TABLE clinics ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
            conn.executescript(SCHEMA)
            if version < 3:
                conn.executemany("UPDATE clinics SET tel_match_key=? WHERE id=?",
                                 [(tel_match_key(row["phone"]), row["id"]) for row in conn.execute("SELECT id,phone FROM clinics")])
            # PRAGMA user_version=N は値が既に同じでも無条件にヘッダを書き込む（SQLiteの仕様）。
            # 既に最新版なら何も書き込まない設計にするため、実際に上げる必要があるときだけ実行する。
            if version < 4:
                conn.execute("PRAGMA user_version=4")

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def setting(self, key, default=None):
        with self.connect() as c:
            r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(r[0]) if r else default

    def set_setting(self, key, value):
        if key not in {"monthly_limit", "external_usage_reserve", "filter_defaults", "age_basis"}:
            raise ValueError("保存できない設定項目です。APIキーは保存できません。")
        with self.connect() as c:
            c.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key,dumps(value)))

    def _get(self, c, cid):
        r = c.execute("SELECT * FROM clinics WHERE id=?", (cid,)).fetchone()
        if not r:
            raise ValueError("医院が見つかりません。")
        if r["merged_into"]:
            return self._get(c, r["merged_into"])
        return {**json.loads(r["effective_json"]), "id": r["id"], "uuid": r["uuid"], "tel_match_key":r["tel_match_key"],
                "first_seen_at":r["first_seen_at"], "last_seen_at":r["last_seen_at"],
                "source_as_of_date":r["source_as_of_date"], "is_new_since_last_update":bool(r["is_new"])}

    def get(self, cid):
        with self.connect() as c:
            return self._get(c, int(cid))

    def _history(self, c, cid, action, before, after, note=""):
        c.execute("INSERT INTO change_history(clinic_id,action,before_json,after_json,note,created_at) VALUES(?,?,?,?,?,?)",
                  (cid,action,dumps(before),dumps(after),note,now()))

    def _project(self, c, cid):
        from src.scoring.research_scoring import finalize_result
        row = c.execute("SELECT * FROM clinics WHERE id=?", (cid,)).fetchone()
        base = json.loads(row["base_json"])
        research = c.execute("SELECT result_json FROM research_results WHERE clinic_id=?", (cid,)).fetchone()
        data = {**base, **(json.loads(research[0]) if research else {})}
        manual = {r["field"]:json.loads(r["value_json"]) for r in c.execute("SELECT * FROM manual_overrides WHERE clinic_id=?", (cid,))}
        data.update(manual)
        data["uuid"] = row["uuid"]
        data["manual_fields"] = list(manual)
        data = finalize_result(data)
        n, p, a = normalize_clinic_name(data.get("clinic_name")), normalize_phone(data.get("phone")), normalize_address(data.get("address"))
        owner = person_from_owner(data.get("owner_name", ""))
        manager = data.get("manager_name", "")
        equal = int(normalize_person(owner)==normalize_person(manager)) if owner and manager else None
        des = parse_date(data.get("designation_date", ""))
        expiry = ""
        if des:
            try:
                expiry = des.replace(year=des.year+10).isoformat()
            except ValueError:
                expiry = des.replace(year=des.year+10,day=28).isoformat()
        active = data.get("status") in {"現存","営業中","開業中","稼働中","true","True","1"} and data.get("facility_type") in {"診療所","クリニック","医院","医科診療所","歯科診療所"}
        data["owner_manager_equal"] = None if equal is None else bool(equal)
        data["active"] = bool(active)
        data["normalized_departments"] = normalize_departments(data.get("departments", ""))
        values = {
            "clinic_name":data.get("clinic_name", ""), "phone":data.get("phone", ""), "address":data.get("address", ""),
            "phone_norm":p, "tel_match_key":tel_match_key(data.get("phone")), "name_norm":n, "name_prefix":n[:2], "address_norm":a, "medical_key":medical_key(data),
            "prefecture":data.get("prefecture", ""), "medical_type":data.get("medical_type", ""), "effective_json":dumps(data),
            "active":int(active), "designation_date":des.isoformat() if des else "", "recent_until":expiry,
            "registration_reason":data.get("registration_reason", ""), "owner_equal":equal,
            "age_probability":data.get("age_probability_under_59"), "hp_status":data.get("hp_status","UNRESEARCHED"),
            "hp_url":data.get("hp_url", ""), "hp_rank":data.get("hp_rank","UNKNOWN"),
            "signal_count":data["marketing_signal_count"], "hot_status":data["hot_status"],
            "departments_json":dumps(data["normalized_departments"]), "treatments_json":dumps(data.get("treatment_categories",[])),
            "signals_json":dumps([s["name"] for s in data.get("marketing_signals",[]) if s.get("status")=="CONFIRMED"]),
            "maps_presence_status":data.get("maps_presence_status",""), "maps_profile_url":data.get("maps_profile_url",""),
            "maps_website_url":data.get("maps_website_url",""), "maps_match_method":data.get("maps_match_method",""),
            "maps_checked_at":data.get("maps_checked_at",""), "exclude_reason":data.get("exclude_reason","")}
        c.execute("UPDATE clinics SET "+",".join(f"{k}=?" for k in values)+" WHERE id=?", (*values.values(),cid))

    def _upsert(self, c, record, match, source, source_hash, row_number, original=None, template_id=None, new_flag=False, existing_source_id=None):
        timestamp = now()
        if match.status == "MATCHED":
            cid = match.candidates[0]
            row = c.execute("SELECT * FROM clinics WHERE id=?",(cid,)).fetchone()
            base = json.loads(row["base_json"])
            before = dict(base)
            # 厚生局の公式情報は内部のベースを更新。コムデスク元行は別表で不変。
            if source == "厚生局" and record.get("as_of", "") >= row["source_as_of_date"]:
                base.update(record)
                if record.get("manager_name") and normalize_person(before.get("manager_name"))!=normalize_person(record["manager_name"]):
                    old_research = c.execute("SELECT result_json FROM research_results WHERE clinic_id=?",(cid,)).fetchone()
                    if old_research:
                        research = json.loads(old_research[0])
                        age_fields = {k for k in research if k.startswith(("age_","license_","graduation_")) or k=="doctor_name"}
                        for key in age_fields:
                            research.pop(key,None)
                        research.update(age_probability_under_59=None,age_estimation_confidence="REVIEW",
                                        age_estimation_reason="管理者が変更されています。現在の院長の経歴を再確認してください。")
                        c.execute("UPDATE research_results SET result_json=?,updated_at=? WHERE clinic_id=?",(dumps(research),timestamp,cid))
            else:
                base.update({k:v for k,v in record.items() if v and not base.get(k)})
            uid = row["uuid"] or record.get("uuid", "")
            c.execute("UPDATE clinics SET base_json=?,uuid=?,last_seen_at=?,source_as_of_date=CASE WHEN ?<source_as_of_date THEN source_as_of_date ELSE ? END WHERE id=?",
                      (dumps(base),uid,timestamp,record.get("as_of",""),record.get("as_of",""),cid))
            if before != base:
                self._history(c,cid,"マスター更新",before,base,match.reason)
        elif match.status == "NEW":
            cid = c.execute("INSERT INTO clinics(uuid,base_json,first_seen_at,last_seen_at,source_as_of_date,is_new) VALUES(?,?,?,?,?,?)",
                            (record.get("uuid",""),dumps(record),timestamp,timestamp,record.get("as_of",""),int(new_flag))).lastrowid
            self._history(c,cid,"医院追加",{},record,match.reason)
        else:
            cid = None
        if existing_source_id is None:
            sr = c.execute("INSERT INTO source_records(clinic_id,source,record_json,source_hash,row_number,match_status,match_reason,match_score,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                           (cid,source,dumps(record),source_hash,row_number,match.status,match.reason,match.score,timestamp)).lastrowid
        else:
            sr = existing_source_id
            c.execute("UPDATE source_records SET clinic_id=?,match_status=?,match_reason=?,match_score=? WHERE id=?",
                      (cid,match.status,match.reason,match.score,sr))
        if original is not None:
            c.execute("INSERT INTO comdesk_original_rows(clinic_id,template_id,row_json,uuid,source_hash,row_number,created_at) VALUES(?,?,?,?,?,?,?)",
                      (cid,template_id,dumps(original),record.get("uuid",""),source_hash,row_number,timestamp))
        if cid is None:
            pending = c.execute("SELECT id FROM match_reviews WHERE source_record_id=? AND status='PENDING'",(sr,)).fetchone()
            if pending:
                c.execute("UPDATE match_reviews SET candidates_json=?,updated_at=? WHERE id=?",(dumps(match.candidates),timestamp,pending[0]))
            else:
                c.execute("INSERT INTO match_reviews(source_record_id,candidates_json,updated_at) VALUES(?,?,?)",(sr,dumps(match.candidates),timestamp))
        else:
            if existing_source_id is not None:
                c.execute("UPDATE match_reviews SET status='RESOLVED',resolved_clinic_id=?,note=?,updated_at=? WHERE source_record_id=? AND status='PENDING'",
                          (cid,"新しい電話番号キーで再照合",timestamp,sr))
            self._project(c,cid)
        return cid

    def import_comdesk(self, table, mapping=None):
        mapping = dict(mapping or infer_comdesk_columns(table))
        if "uuid" not in mapping:
            hits = [i for i,h in enumerate(table.headers) if h.strip().casefold() in {"uuid","案件id","管理id","リードid","lead_id","id"}]
            mapping["uuid"] = hits[0] if len(hits)==1 else None
        if mapping.get("clinic_name") is None:
            raise ValueError("医院名の列を指定してください。")
        if not all(v is None or isinstance(v,int) and 0<=v<len(table.headers) for v in mapping.values()):
            raise ValueError("対応する列番号を確認してください。")
        rows = table.data.values.tolist()
        batch = digest(["comdesk",table.headers,mapping,rows])
        template = digest([table.headers,mapping])
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            old = c.execute("SELECT result_json FROM import_batches WHERE id=?",(batch,)).fetchone()
            if old:
                return {**json.loads(old[0]),"already_imported":True}
            c.execute("INSERT OR IGNORE INTO templates VALUES(?,?,?,?)",(template,dumps(table.headers),dumps(mapping),now()))
            counts = {"MATCHED":0,"NEW":0,"AMBIGUOUS":0,"template_id":template}
            for i, raw in enumerate(rows):
                record = record_from_row(raw, mapping)
                if not record.get("clinic_name", "").strip():
                    raise ValueError(f"{i+2}行目の医院名が空白です。取込は取り消しました。")
                match = match_record(c,record)
                self._upsert(c,record,match,"コムデスク",batch,i,raw,template)
                counts[match.status] += 1
            c.execute("INSERT INTO import_batches VALUES(?,?,?,?)",(batch,"コムデスク",dumps(counts),now()))
            return counts

    def import_master(self, frame):
        records = frame.fillna("").astype(str).to_dict("records") if isinstance(frame,pd.DataFrame) else frame
        if not records:
            raise ValueError("取り込む厚生局データがありません。")
        for i,r in enumerate(records):
            if not r.get("clinic_name") or not r.get("prefecture") or r.get("medical_type") not in {"医科","歯科"} or not parse_date(r.get("as_of")):
                raise ValueError(f"厚生局データ{i+2}行目：医院名・都道府県・医科/歯科・基準日を確認してください。")
        batch = digest(["master",records])
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            old = c.execute("SELECT result_json FROM import_batches WHERE id=?",(batch,)).fetchone()
            if old:
                return {**json.loads(old[0]),"already_imported":True}
            scopes = {(r["prefecture"],r["medical_type"],r["as_of"]) for r in records}
            for pref,med,asof in scopes:
                latest = c.execute("SELECT MAX(source_as_of_date) FROM clinics WHERE prefecture=? AND medical_type=?",(pref,med)).fetchone()[0]
                if latest and asof < latest:
                    raise ValueError("DBより古い厚生局データです。更新日を確認してください。取込は取り消しました。")
                if not latest or asof > latest:
                    c.execute("UPDATE clinics SET is_new=0 WHERE prefecture=? AND medical_type=?",(pref,med))
            counts = {"MATCHED":0,"NEW":0,"AMBIGUOUS":0}
            for i,record in enumerate(records):
                match = match_record(c,record)
                self._upsert(c,record,match,"厚生局",batch,i,new_flag=True)
                counts[match.status] += 1
            c.execute("INSERT INTO import_batches VALUES(?,?,?,?)",(batch,"厚生局",dumps(counts),now()))
            return counts

    def save_research(self, cid, result, pages=None):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            cid = self._get(c,cid)["id"]
            row = c.execute("SELECT result_json FROM research_results WHERE clinic_id=?",(cid,)).fetchone()
            previous = json.loads(row[0]) if row else {}
            updated = {**previous, **result}
            c.execute("INSERT OR REPLACE INTO research_results VALUES(?,?,?)",(cid,dumps(updated),now()))
            if pages is not None:
                c.execute("DELETE FROM hp_pages WHERE clinic_id=?",(cid,))
                for p in pages:
                    c.execute("INSERT OR REPLACE INTO hp_pages VALUES(?,?,?,?)",(cid,p["url"],dumps(p),now()))
            self._history(c,cid,"自動調査",previous,updated)
            self._project(c,cid)

    def override(self, cid, field, value, note="", source="手動確認"):
        from src.scoring.research_scoring import SIGNAL_NAMES
        allowed = {"hp_url","hp_status","hp_rank","epark_url","epark_contract","google_ads_status","treatment_categories","marketing_signals"}
        if field not in allowed:
            raise ValueError("手動修正の対象外です。")
        if value is not None:
            enums = {"hp_status":{"VERIFIED","REVIEW","NOT_FOUND","ERROR","UNRESEARCHED"},
                     "hp_rank":{"A","B","C","D","NO_HP","UNKNOWN"},"epark_contract":{"PAID","FREE","UNKNOWN"},
                     "google_ads_status":{"CONFIRMED","NOT_CONFIRMED","UNKNOWN"}}
            if field in enums and value not in enums[field]:
                raise ValueError("選択値を確認してください。")
            if field.endswith("url") and value:
                from src.enrichment.safe_web import validate_url
                validate_url(value, resolve=False)
                if field == "epark_url":
                    from src.enrichment.epark_checker import canonical_epark_url
                    if not canonical_epark_url(value):
                        raise ValueError("EPARKの医院ページURLを入力してください。")
            if field in {"treatment_categories","marketing_signals"} and not isinstance(value,list):
                raise ValueError("複数選択の値を確認してください。")
            if field=="marketing_signals" and any(v not in SIGNAL_NAMES for v in value):
                raise ValueError("未定義の集客投資シグナルです。")
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            cid = self._get(c,cid)["id"]
            before = c.execute("SELECT value_json FROM manual_overrides WHERE clinic_id=? AND field=?",(cid,field)).fetchone()
            if value is None:
                c.execute("DELETE FROM manual_overrides WHERE clinic_id=? AND field=?",(cid,field))
            else:
                c.execute("INSERT OR REPLACE INTO manual_overrides VALUES(?,?,?,?,?,?)",(cid,field,dumps(value),source,note,now()))
            self._history(c,cid,"手動修正:"+field,json.loads(before[0]) if before else None,value,note)
            self._project(c,cid)

    def count(self, filters=None, as_of=None):
        sql,args = where(filters or Filters(active_only=False,hp_only=False),as_of)
        with self.connect() as c:
            return c.execute("SELECT count(*) FROM clinics WHERE "+sql,args).fetchone()[0]

    def query(self, filters=None, limit=100, offset=0, as_of=None):
        filters = filters or Filters(active_only=False,hp_only=False)
        sql,args = where(filters,as_of)
        with self.connect() as c:
            rows = c.execute("SELECT id FROM clinics WHERE "+sql+" ORDER BY signal_count DESC,id LIMIT ? OFFSET ?",(*args,min(100000,max(0,int(limit))),max(0,int(offset)))).fetchall()
            records = [self._get(c,r[0]) for r in rows]
        if filters.sales_pairs:
            from src.master.sales_treatments import matching_pairs
            for record in records:
                record["matched_pairs"] = [
                    {
                        "department": match.department, "treatment": match.treatment,
                        "matched_category": match.matched_category,
                        "matched_keyword": match.matched_keyword,
                        "evidence_source": match.evidence_source,
                    }
                    for match in matching_pairs(
                        record.get("normalized_departments", ()),
                        record.get("treatment_categories", ()),
                        record.get("treatment_evidence", ()),
                        filters.sales_pairs,
                    )
                ]
        return records

    def funnel(self, filters, as_of=None):
        conditions,args = ["merged_into IS NULL", "merge_hold=0"],[]
        with self.connect() as c:
            output = [("全マスター",c.execute("SELECT count(*) FROM clinics WHERE merged_into IS NULL").fetchone()[0])]
            output.append(("重複確認待ちを除く",c.execute("SELECT count(*) FROM clinics WHERE merged_into IS NULL AND merge_hold=0").fetchone()[0]))
            for label,sql,params in clauses(filters,as_of):
                conditions.append(sql); args.extend(params)
                output.append((label,c.execute("SELECT count(*) FROM clinics WHERE "+" AND ".join(conditions),args).fetchone()[0]))
            output.append(("最終営業対象",output[-1][1]))
            return output

    def metrics(self):
        items = {"全マスター":"1", "既存UUIDあり":"uuid<>''", "新規医院（UUIDなし）":"uuid=''", "現存クリニック":"active=1",
                 "HP確認済み":"hp_status='VERIFIED'", "HP未発見":"hp_status='NOT_FOUND'", "年齢推定済み":"age_probability IS NOT NULL",
                 "HP要確認":"hp_status IN ('REVIEW','ERROR')", "アツい（2個以上）":"signal_count>=2", "かなりアツい":"signal_count>=3"}
        with self.connect() as c:
            result = {label:c.execute("SELECT count(*) FROM clinics WHERE merged_into IS NULL AND "+sql).fetchone()[0] for label,sql in items.items()}
            result["要確認重複（保留レコード）"] = c.execute("SELECT count(*) FROM match_reviews WHERE status='PENDING'").fetchone()[0]
            result["厚生局取込レコード"] = c.execute("SELECT count(*) FROM source_records WHERE source='厚生局'").fetchone()[0]
            result["Google Maps掲載確認"] = c.execute("SELECT count(*) FROM clinics WHERE merged_into IS NULL AND maps_presence_status IN ('MAPS_MATCHED_WEBSITE','MAPS_MATCHED_NO_WEBSITE')").fetchone()[0]
            result["Google Maps HP取得"] = c.execute("SELECT count(*) FROM clinics WHERE merged_into IS NULL AND maps_presence_status='MAPS_MATCHED_WEBSITE' AND maps_website_url<>''").fetchone()[0]
            result["Google Maps未発見"] = c.execute("SELECT count(*) FROM clinics WHERE merged_into IS NULL AND maps_presence_status='MAPS_NOT_FOUND'").fetchone()[0]
            return result

    def templates(self):
        with self.connect() as c:
            return [{"id":r["id"],"headers":json.loads(r["headers_json"]),"mapping":json.loads(r["mapping_json"])} for r in c.execute("SELECT * FROM templates ORDER BY created_at,id")]

    def google_maps_queue_csv(self):
        from src.master.google_maps import queue_csv
        # Mapsの母集団は厚生局。Comdeskだけの行を勝手に検索対象へ混ぜない。
        with self.connect() as c:
            ids = [r[0] for r in c.execute("SELECT DISTINCT clinic_id FROM source_records WHERE source='厚生局' AND clinic_id IS NOT NULL ORDER BY clinic_id")]
            records = []
            seen = set()
            for cid in ids:
                rec = self._get(c, cid)
                if rec["id"] in seen:
                    continue
                seen.add(rec["id"])
                records.append(rec)
        return queue_csv(records)

    def import_google_maps(self, frame):
        from src.master.google_maps import import_maps_results
        return import_maps_results(self, frame)

    def export_management_csv(self):
        rows=self.query(Filters(active_only=False,hp_only=False),limit=100000)
        keys=sorted({k for r in rows for k in r.keys()})
        return csv_bytes(keys, [[r.get(k,"") if not isinstance(r.get(k), (list,dict)) else dumps(r.get(k)) for k in keys] for r in rows])

    def export(self, filters, template_id=None, as_of=None):
        # template_idは既存呼び出しとの互換用。出力は入力形式によらず28列。
        from src.master.fixed_export import export_fixed
        return export_fixed(self, filters, as_of)

    def reviews(self, limit=100):
        with self.connect() as c:
            return [dict(r) for r in c.execute("SELECT m.*,s.record_json,s.source,s.match_reason,s.match_score FROM match_reviews m JOIN source_records s ON s.id=m.source_record_id WHERE m.status='PENDING' ORDER BY m.id LIMIT ?",(limit,))]

    def reintegrate_existing(self):
        from src.master.reintegration import reintegrate_existing
        return reintegrate_existing(self)

    def resolve_review(self, review_id, target_id=None, note=""):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            r = c.execute("SELECT m.*,s.record_json,s.source,s.source_hash,s.row_number,s.clinic_id AS existing_clinic_id FROM match_reviews m JOIN source_records s ON m.source_record_id=s.id WHERE m.id=? AND m.status='PENDING'",(review_id,)).fetchone()
            if not r:
                raise ValueError("この確認事項は処理済みです。")
            record = json.loads(r["record_json"])
            if r["existing_clinic_id"] is not None:
                from filelock import FileLock, Timeout
                from src.master.reintegration import merge_clinics
                cid = self._get(c,r["existing_clinic_id"])["id"]
                try:
                    with FileLock(str(self.path)+".research.lock",timeout=0):
                        target_id = cid if target_id is None else merge_clinics(self,c,cid,target_id,"手動確認: "+note)
                except Timeout as exc:
                    raise ValueError("自動調査を一時停止してから重複確認を保存してください。") from exc
                c.execute("UPDATE clinics SET merge_hold=0 WHERE id=?",(target_id,))
                c.execute("UPDATE source_records SET clinic_id=?,match_status='MATCHED',match_reason=? WHERE id=?",(target_id,"手動確認: "+note,r["source_record_id"]))
                c.execute("UPDATE match_reviews SET status='RESOLVED',resolved_clinic_id=?,note=?,updated_at=? WHERE id=?",(target_id,note,now(),review_id))
                self._history(c,target_id,"重複確認",{"review_id":review_id},{"target_id":target_id},note)
                return target_id
            if target_id is None:
                uid = record.get("uuid", "")
                if uid and c.execute("SELECT 1 FROM clinics WHERE uuid=? AND merged_into IS NULL",(uid,)).fetchone():
                    raise ValueError("同じUUIDが既にあります。別医院として追加できません。")
                target_id = c.execute("INSERT INTO clinics(uuid,base_json,first_seen_at,last_seen_at,source_as_of_date,is_new) VALUES(?,?,?,?,?,1)",
                                      (uid,dumps(record),now(),now(),record.get("as_of",""))).lastrowid
            else:
                target_id = self._get(c,target_id)["id"]
                row = c.execute("SELECT * FROM clinics WHERE id=?",(target_id,)).fetchone()
                if row["uuid"] and record.get("uuid") and row["uuid"]!=record["uuid"]:
                    raise ValueError("異なる既存UUID同士は統合できません。元のコムデスクで確認してください。")
                base = json.loads(row["base_json"])
                if r["source"]=="厚生局":
                    base.update(record)
                else:
                    base.update({k:v for k,v in record.items() if v and not base.get(k)})
                c.execute("UPDATE clinics SET uuid=?,base_json=?,last_seen_at=? WHERE id=?",(row["uuid"] or record.get("uuid",""),dumps(base),now(),target_id))
            c.execute("UPDATE source_records SET clinic_id=?,match_status='MATCHED',match_reason=? WHERE id=?",(target_id,"手動確認: "+note,r["source_record_id"]))
            c.execute("UPDATE comdesk_original_rows SET clinic_id=? WHERE source_hash=? AND row_number=?",(target_id,r["source_hash"],r["row_number"]))
            c.execute("UPDATE match_reviews SET status='RESOLVED',resolved_clinic_id=?,note=?,updated_at=? WHERE id=?",(target_id,note,now(),review_id))
            self._history(c,target_id,"重複確認",{"review_id":review_id},{"target_id":target_id},note)
            self._project(c,target_id)
            return target_id

    def history(self, cid, limit=30):
        with self.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM change_history WHERE clinic_id=? ORDER BY id DESC LIMIT ?",(cid,limit))]

    def revision(self):
        with self.connect() as c:
            return c.execute("SELECT COALESCE(MAX(id),0) FROM change_history").fetchone()[0]

    def refresh_age_model(self):
        from src.utils.config import ROOT
        from src.enrichment.profiles import estimate_profile_age
        signature = digest([today_japan().year,(ROOT/"config/age_model.yml").read_text(encoding="utf-8")])
        if self.setting("age_basis")==signature:
            return
        with self.connect() as c:
            ids = [r[0] for r in c.execute("SELECT clinic_id FROM research_results WHERE json_extract(result_json,'$.license_registration_year') IS NOT NULL OR json_extract(result_json,'$.graduation_year') IS NOT NULL")]
        for cid in ids:
            self.save_research(cid,estimate_profile_age(self.get(cid)))
        self.set_setting("age_basis",signature)

    def backup_bytes(self):
        fd,path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        try:
            with self.connect() as source:
                dest = sqlite3.connect(path)
                try:
                    source.backup(dest)
                finally:
                    dest.close()
            return Path(path).read_bytes()
        finally:
            Path(path).unlink(missing_ok=True)
