"""Phase 4: 13,970-legacy x MHLW JOIN dry run. READ ONLY. Never writes to clinics.sqlite3.

Uses the EXISTING production normalizers (src.normalizer.clinic_name / address) so the
join logic is consistent with how the running system already dedups 厚生局/コムデスク data.

Rule (per explicit user instruction):
  - Only a mutually-unique 1:1 match on (name_norm, address_norm) auto-confirms as NAME_ADDRESS.
  - 1:many / many:1 / many:many on that combined key -> AMBIGUOUS_REVIEW.
  - No combined match, but name_norm-only or address_norm-only match exists -> FUZZY_REVIEW.
  - No match of any kind -> UNMATCHED.
No fuzzy/approximate string scoring is used to auto-confirm anything.
"""
import csv
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
csv.field_size_limit(sys.maxsize)

from src.normalizer.clinic_name import normalize_clinic_name
from src.normalizer.address import normalize_address
from mhlw_dry_run.paths import BASE, ROOT, source

DB_PATH = ROOT / "data" / "clinics.sqlite3"
FACILITY_FILES = [
    ("医科", source("02-1_clinic_facility_info_20260601.csv")),
    ("歯科", source("03-1_dental_facility_info_20260601.csv")),
]
OUT_CSV = BASE / "legacy_mhlw_join.csv"
OUT_JSON = BASE / "join_summary.json"

# ---- 1. Build MHLW facility indices ----
mhlw_facilities = []  # (mhlw_facility_id, facility_type, clinic_name, address, name_norm, address_norm)
for facility_type, path in FACILITY_FILES:
    with open(path, encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            name = row["正式名称"]
            addr = row["所在地"]
            mhlw_facilities.append((row["ID"], facility_type, name, addr,
                                     normalize_clinic_name(name), normalize_address(addr)))

combined_index = defaultdict(list)
name_index = defaultdict(list)
address_index = defaultdict(list)
for fid, ftype, name, addr, n, a in mhlw_facilities:
    if n and a:
        combined_index[(n, a)].append(fid)
    if n:
        name_index[n].append(fid)
    if a:
        address_index[a].append(fid)

print(f"MHLW facilities indexed: {len(mhlw_facilities)}", file=sys.stderr)

# ---- 2. Read legacy clinics (READ ONLY) ----
conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
conn.execute("PRAGMA query_only=ON")
conn.row_factory = sqlite3.Row
legacy = conn.execute("SELECT id, uuid, clinic_name, address, medical_key FROM clinics").fetchall()
conn.close()
print(f"legacy clinics read: {len(legacy)}", file=sys.stderr)

legacy_rows = []
legacy_key_count = defaultdict(int)
for row in legacy:
    n = normalize_clinic_name(row["clinic_name"])
    a = normalize_address(row["address"])
    legacy_rows.append({"id": row["id"], "uuid": row["uuid"], "clinic_name": row["clinic_name"],
                         "address": row["address"], "name_norm": n, "address_norm": a})
    if n and a:
        legacy_key_count[(n, a)] += 1

# ---- 3. Match ----
counts = {"EXACT_ID": 0, "NAME_ADDRESS": 0, "AMBIGUOUS_REVIEW": 0, "FUZZY_REVIEW": 0, "UNMATCHED": 0}
out_rows = []
for lr in legacy_rows:
    n, a = lr["name_norm"], lr["address_norm"]
    confidence = "UNMATCHED"
    matched_ids = []
    match_note = ""
    if n and a:
        combined_candidates = combined_index.get((n, a), [])
        legacy_dupe = legacy_key_count.get((n, a), 0)
        if len(combined_candidates) == 1 and legacy_dupe == 1:
            confidence = "NAME_ADDRESS"
            matched_ids = combined_candidates
            match_note = "医院名正規化+住所正規化が両側で一意に一致"
        elif combined_candidates or legacy_dupe > 1:
            confidence = "AMBIGUOUS_REVIEW"
            matched_ids = combined_candidates
            match_note = f"MHLW側候補{len(combined_candidates)}件 / レガシー側同一キー{legacy_dupe}件（1:1でないため自動確定せずREVIEW）"
    if confidence == "UNMATCHED":
        name_hits = name_index.get(n, []) if n else []
        addr_hits = address_index.get(a, []) if a else []
        if name_hits or addr_hits:
            confidence = "FUZZY_REVIEW"
            matched_ids = sorted(set(name_hits) | set(addr_hits))[:20]
            reasons = []
            if name_hits:
                reasons.append(f"医院名のみ一致{len(name_hits)}件")
            if addr_hits:
                reasons.append(f"住所のみ一致{len(addr_hits)}件")
            match_note = " / ".join(reasons) + "（自動確定不可のためREVIEW）"
    counts[confidence] += 1
    out_rows.append({
        "clinic_id": lr["id"], "clinic_uuid": lr["uuid"], "clinic_name": lr["clinic_name"], "address": lr["address"],
        "name_norm": n, "address_norm": a, "confidence": confidence,
        "mhlw_facility_id_candidates": ";".join(matched_ids), "candidate_count": len(matched_ids), "match_note": match_note,
    })

with open(OUT_CSV, "w", encoding="utf-8", newline="") as out:
    w = csv.DictWriter(out, fieldnames=["clinic_id", "clinic_uuid", "clinic_name", "address", "name_norm", "address_norm",
                                         "confidence", "mhlw_facility_id_candidates", "candidate_count", "match_note"])
    w.writeheader()
    w.writerows(out_rows)

total = len(legacy_rows)
matched = counts["NAME_ADDRESS"]
summary = {
    "legacy_total": total,
    "matched": matched,
    "exact_id": counts["EXACT_ID"],
    "exact_phone": 0,
    "name_postal": 0,
    "name_address": counts["NAME_ADDRESS"],
    "ambiguous_review": counts["AMBIGUOUS_REVIEW"],
    "fuzzy_review": counts["FUZZY_REVIEW"],
    "unmatched": counts["UNMATCHED"],
    "matched_rate": round(matched / total, 4) if total else 0,
    "notes": {
        "exact_id": "厚生局clinic_id体系とMHLW医療情報ネットID体系は別体系のため実行不可(Phase1監査で確認)",
        "exact_phone": "MHLW側CSVに電話番号カラムが存在しないため実行不可(Phase1監査で確認)",
        "name_postal": "MHLW側CSVに郵便番号カラムが存在しないため実行不可(Phase1監査で確認)",
        "auto_confirm_rule": "name_norm+address_normが両側(MHLW側・レガシー側)で一意な1:1一致のみ自動確定。1:多/多:1/多:多はAMBIGUOUS_REVIEW、名称のみ/住所のみ一致はFUZZY_REVIEWへ。",
    },
}
with open(OUT_JSON, "w", encoding="utf-8") as out:
    json.dump(summary, out, ensure_ascii=False, indent=2)

print(json.dumps(summary, ensure_ascii=False, indent=2), file=sys.stderr)
