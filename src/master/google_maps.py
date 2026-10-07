"""Google Maps収集結果の取込と、Maps調査キュー出力。Comdesk出力とは分離する。"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib, json, re, unicodedata
import pandas as pd
from rapidfuzz import fuzz
from src.normalizer.phone import tel_match_key
from src.normalizer.address import normalize_address
from src.normalizer.clinic_name import normalize_clinic_name
from src.io.output_writer import csv_bytes

MAPS_RESULT_HEADERS = [
    "internal_clinic_id","medical_institution_number","source_clinic_name","source_phone","source_address","source_prefecture",
    "maps_match_status","maps_match_method","maps_name","maps_phone","maps_address","maps_profile_url","maps_website_url",
    "website_status","phone_match","name_match","address_match","exclude_reason","scrape_status","scraped_at",
    "休診日","診療日","午前始","午前終","午後始","午後終","営業時間原文",
]
QUEUE_HEADERS = ["internal_clinic_id","medical_institution_number","clinic_name","phone","address","prefecture","facility_type","status"]


def _s(v):
    return "" if v is None or (isinstance(v,float) and pd.isna(v)) else str(v).strip()


def excluded_reason(record):
    name = unicodedata.normalize("NFKC", _s(record.get("clinic_name")))
    facility = unicodedata.normalize("NFKC", _s(record.get("facility_type")))
    if facility == "病院" or "病院" in name:
        return "hospital"
    if "センター" in name:
        return "center"
    return ""


def queue_csv(records):
    rows=[]
    for r in records:
        rows.append([
            r.get("id", ""), r.get("medical_institution_number", "") or r.get("clinic_id", ""), r.get("clinic_name", ""),
            r.get("phone", ""), r.get("address", ""), r.get("prefecture", ""), r.get("facility_type", ""), r.get("status", ""),
        ])
    return csv_bytes(QUEUE_HEADERS, rows)


def _name_score(a,b):
    aa,bb=normalize_clinic_name(_s(a)),normalize_clinic_name(_s(b))
    return fuzz.ratio(aa,bb) if aa and bb else 0


def _addr_score(a,b):
    aa,bb=normalize_address(_s(a)),normalize_address(_s(b))
    return fuzz.ratio(aa,bb) if aa and bb else 0


def validate_maps_frame(frame: pd.DataFrame) -> pd.DataFrame:
    frame=frame.fillna("").astype(str)
    missing=[h for h in MAPS_RESULT_HEADERS if h not in frame.columns]
    if missing:
        raise ValueError("Google Maps取得結果CSVの列が不足しています: "+", ".join(missing))
    return frame[MAPS_RESULT_HEADERS].copy()


def find_target(c, row):
    internal=_s(row.get("internal_clinic_id"))
    if internal.isdigit():
        hit=c.execute("SELECT id FROM clinics WHERE id=? AND merged_into IS NULL",(int(internal),)).fetchone()
        if hit: return int(hit[0]),"internal_clinic_id",100
    med=_s(row.get("medical_institution_number"))
    if med:
        hits=c.execute("SELECT id FROM clinics WHERE merged_into IS NULL AND json_extract(effective_json,'$.medical_institution_number')=?",(med,)).fetchall()
        if len(hits)==1:return int(hits[0][0]),"medical_institution_number",100
        hits=c.execute("SELECT id FROM clinics WHERE merged_into IS NULL AND json_extract(effective_json,'$.clinic_id')=?",(med,)).fetchall()
        if len(hits)==1:return int(hits[0][0]),"medical_institution_number",100
    tk=tel_match_key(_s(row.get("source_phone"))) or tel_match_key(_s(row.get("maps_phone")))
    if tk:
        hits=c.execute("SELECT id,name_norm,address_norm FROM clinics WHERE merged_into IS NULL AND tel_match_key=?",(tk,)).fetchall()
        if len(hits)==1:return int(hits[0][0]),"tel_match_key",100
        if len(hits)>1:
            scored=[]
            for h in hits:
                rec=c.execute("SELECT effective_json FROM clinics WHERE id=?",(h[0],)).fetchone()
                d=json.loads(rec[0]);scored.append((_name_score(row.get("source_clinic_name"),d.get("clinic_name"))+_addr_score(row.get("source_address"),d.get("address")),int(h[0])))
            scored.sort(reverse=True)
            if scored and (len(scored)==1 or scored[0][0]-scored[1][0]>=30):return scored[0][1],"tel_match_key",scored[0][0]/2
            return None,"AMBIGUOUS",0
    name=normalize_clinic_name(_s(row.get("source_clinic_name")))
    addr=normalize_address(_s(row.get("source_address")))
    if name and addr:
        hits=c.execute("SELECT id FROM clinics WHERE merged_into IS NULL AND name_norm=? AND address_norm=?",(name,addr)).fetchall()
        if len(hits)==1:return int(hits[0][0]),"name_address",100
    return None,"NOT_FOUND",0


def classify_match_status(maps_status):
    """Pure: which `import_maps_results` counts bucket this row's maps_match_status belongs
    to, independent of whether a clinic match (cid) was found. Extracted so the Supabase write
    adapter (src/repository/supabase_write_adapter.py) can share this exact classification
    instead of re-deriving it -- no behavior change, counts computation is unchanged below.
    """
    maps_status = _s(maps_status)
    if maps_status.startswith("EXCLUDED_"):
        return "EXCLUDED"
    if maps_status == "MAPS_NOT_FOUND":
        return "NOT_FOUND"
    if maps_status == "MAPS_AMBIGUOUS":
        return "AMBIGUOUS"
    if maps_status == "ERROR":
        return "ERROR"
    if maps_status.startswith("MAPS_MATCHED"):
        return "WEBSITE" if maps_status == "MAPS_MATCHED_WEBSITE" else "NO_WEBSITE"
    return None


_PRESERVED_KEYS = (
    "maps_presence_status", "maps_profile_url", "maps_website_url", "maps_match_method",
    "maps_checked_at", "maps_name", "maps_phone", "maps_address",
    "maps_regular_holiday", "maps_business_days", "maps_morning_start", "maps_morning_end",
    "maps_afternoon_start", "maps_afternoon_end", "maps_hours_raw", "exclude_reason",
)


def build_maps_update(row, base, maps_status, method):
    """Pure: given the clinic's current `base` dict and one incoming Maps result `row`, returns
    (maps_update_dict, preserved_confirmed_website: bool). Extracted verbatim from
    `import_maps_results` (see module docstring at classify_match_status) -- no behavior change;
    `import_maps_results` below calls this instead of inlining the same computation, and the
    Supabase write adapter imports and calls this exact function too (same anti-downgrade rule
    on both backends, by construction, not by independent reimplementation).
    """
    existing_confirmed_website = (
        _s(base.get("maps_presence_status")) == "MAPS_MATCHED_WEBSITE"
        and bool(_s(base.get("maps_website_url")))
    )
    incoming_website_status = _s(row.get("website_status")).upper()
    incoming_confirmed_website = (
        maps_status == "MAPS_MATCHED_WEBSITE"
        and bool(_s(row.get("maps_website_url")))
        and "AMBIGUOUS" not in incoming_website_status
        and "NO_WEBSITE" not in incoming_website_status
        and "ERROR" not in incoming_website_status
    )
    preserve_confirmed_website = existing_confirmed_website and not incoming_confirmed_website
    maps_update = {
        "maps_presence_status": maps_status, "maps_profile_url": _s(row.get("maps_profile_url")),
        "maps_website_url": _s(row.get("maps_website_url")),
        "maps_match_method": _s(row.get("maps_match_method")) or method, "maps_checked_at": _s(row.get("scraped_at")),
        "maps_name": _s(row.get("maps_name")), "maps_phone": _s(row.get("maps_phone")), "maps_address": _s(row.get("maps_address")),
        "maps_regular_holiday": _s(row.get("休診日")), "maps_business_days": _s(row.get("診療日")),
        "maps_morning_start": _s(row.get("午前始")), "maps_morning_end": _s(row.get("午前終")),
        "maps_afternoon_start": _s(row.get("午後始")), "maps_afternoon_end": _s(row.get("午後終")),
        "maps_hours_raw": _s(row.get("営業時間原文")), "exclude_reason": _s(row.get("exclude_reason")),
    }
    if preserve_confirmed_website:
        for key in _PRESERVED_KEYS:
            maps_update[key] = base.get(key, "")
    return maps_update, preserve_confirmed_website


def import_maps_results(store, frame: pd.DataFrame):
    frame=validate_maps_frame(frame)
    records=frame.to_dict("records")
    batch=hashlib.sha256(json.dumps(records,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    counts={"TOTAL":len(records),"MATCHED":0,"WEBSITE":0,"NO_WEBSITE":0,"NOT_FOUND":0,"AMBIGUOUS":0,"EXCLUDED":0,"ERROR":0,"UNLINKED":0,"PRESERVED_WEBSITE":0}
    with store.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        old=c.execute("SELECT result_json FROM import_batches WHERE id=?",(batch,)).fetchone()
        if old:return {**json.loads(old[0]),"already_imported":True}
        for i,row in enumerate(records, start=2):
            cid,method,score=find_target(c,row)
            maps_status=_s(row.get("maps_match_status"))
            bucket = classify_match_status(maps_status)
            if bucket in ("WEBSITE", "NO_WEBSITE"):
                counts["MATCHED"]+=1
                counts[bucket]+=1
            elif bucket is not None:
                counts[bucket]+=1
            if cid is None: counts["UNLINKED"]+=1
            raw=json.dumps(row,ensure_ascii=False,sort_keys=True,separators=(",",":"))
            c.execute("INSERT INTO google_maps_results(clinic_id,batch_id,row_number,result_json,maps_match_status,maps_match_method,maps_profile_url,maps_website_url,scraped_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (cid,batch,i,raw,maps_status,_s(row.get("maps_match_method")) or method,_s(row.get("maps_profile_url")),_s(row.get("maps_website_url")),_s(row.get("scraped_at")),datetime.now(timezone.utc).isoformat()))
            if cid is not None:
                clinic=c.execute("SELECT base_json FROM clinics WHERE id=?",(cid,)).fetchone();base=json.loads(clinic[0])
                before=dict(base)

                # 既にGoogle Mapsで確認済みのHP URLがある医院は、後続バッチの
                # NO_WEBSITE / AMBIGUOUS / NOT_FOUND / ERROR / URL空欄で格下げしない。
                # 各バッチの生データは google_maps_results にそのまま保存されるため、
                # 最新調査の事実は失わず、医院マスターの「確定済みHP」だけを保護する。
                maps_update, preserve_confirmed_website = build_maps_update(row, base, maps_status, method)
                if preserve_confirmed_website:
                    counts["PRESERVED_WEBSITE"] += 1
                base.update(maps_update)
                c.execute("UPDATE clinics SET base_json=? WHERE id=?",(json.dumps(base,ensure_ascii=False,sort_keys=True,separators=(",",":")),cid))
                if before!=base: store._history(c,cid,"Google Maps取込",before,base,_s(row.get("maps_match_method")) or method)
                store._project(c,cid)
        c.execute("INSERT INTO import_batches VALUES(?,?,?,?)",(batch,"Google Maps",json.dumps(counts,ensure_ascii=False,sort_keys=True,separators=(",",":")),datetime.now(timezone.utc).isoformat()))
    return counts
