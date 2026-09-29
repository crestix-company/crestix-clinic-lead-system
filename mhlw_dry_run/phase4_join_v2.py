"""Phase 4.1 v2: 13,970 legacy x MHLW JOIN精度改善(READ ONLY dry run)。

既存の legacy_mhlw_join.csv / clinic_mhlw_departments.sqlite3 は一切上書きしない。
Production DB(./data/clinics.sqlite3)はmode=ro+PRAGMA query_only=ONでのみ開く。

Rule順序(すべて両側1:1確定のみ自動MATCH。fuzzy scoreでの自動確定は一切行わない):
  A. NAME_ADDRESS_EXACT      : 既存Phase4の結果をそのまま踏襲(再計算しない)
  B. NORMALIZED_NAME_ADDRESS : light_name(法人種別を残す軽量正規化) + full_address が1:1
  C. UNIQUE_ADDRESS_MATCH    : full_addressのみで1:1(名称不問。name_mismatchを記録)
  D. BASE_ADDRESS_CORP_NAME  : base_address(ビル名除去) + comparison_name(法人種別除去)が1:1

いずれのRuleも「候補が2件以上」「片側でも重複」の場合はMATCHEDにせずREVIEWへ残す。
"""
import csv
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
csv.field_size_limit(sys.maxsize)

from src.normalizer.clinic_name import normalize_clinic_name  # noqa: E402  (Rule A用・既存関数)
from src.normalizer.address import normalize_address  # noqa: E402
from mhlw_dry_run.normalize_v2 import light_normalize_name, comparison_name, base_address  # noqa: E402
from mhlw_dry_run.paths import source  # noqa: E402

BASE = Path(__file__).resolve().parent
DB_PATH = Path(__file__).resolve().parents[1] / "data" / "clinics.sqlite3"
FACILITY_FILES = [
    ("医科", source("02-1_clinic_facility_info_20260601.csv")),
    ("歯科", source("03-1_dental_facility_info_20260601.csv")),
]

OUT_CSV = BASE / "phase4_join_v2.csv"
OUT_JSON = BASE / "join_summary_v2.json"
OUT_SIDECAR = BASE / "clinic_mhlw_departments_v2.sqlite3"
OUT_AUDIT = BASE / "unique_address_match_audit.csv"


def log(*a):
    print(*a, file=sys.stderr)


# ---------- 1. MHLW facility index ----------
mhlw = {}  # facility_id -> dict of all fields
for ftype, path in FACILITY_FILES:
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            fid = row["ID"]
            name_raw, addr_raw = row["正式名称"], row["所在地"]
            full = normalize_address(addr_raw)
            mhlw[fid] = {
                "mhlw_facility_id": fid, "facility_type": ftype,
                "name_raw": name_raw, "addr_raw": addr_raw,
                "name_existing_norm": normalize_clinic_name(name_raw),  # Rule Aと同一関数(参照用)
                "name_light": light_normalize_name(name_raw),
                "name_comparison": comparison_name(name_raw),
                "addr_full": full,
                "addr_base": base_address(full, addr_raw),
            }
log(f"MHLW facilities indexed: {len(mhlw)}")

# ---------- 2. legacy clinics (READ ONLY) ----------
conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
conn.execute("PRAGMA query_only=ON")
conn.row_factory = sqlite3.Row
legacy_rows = conn.execute("SELECT id, uuid, clinic_name, address, medical_type FROM clinics").fetchall()
db_count_check = conn.execute("SELECT count(*) FROM clinics").fetchone()[0]
conn.close()

legacy = {}
for r in legacy_rows:
    cid = str(r["id"])
    full = normalize_address(r["address"])
    legacy[cid] = {
        "clinic_id": cid, "clinic_uuid": r["uuid"], "medical_type": r["medical_type"],
        "name_raw": r["clinic_name"], "addr_raw": r["address"],
        "name_existing_norm": normalize_clinic_name(r["clinic_name"]),
        "name_light": light_normalize_name(r["clinic_name"]),
        "name_comparison": comparison_name(r["clinic_name"]),
        "addr_full": full,
        "addr_base": base_address(full, r["address"]),
    }
log(f"legacy clinics read: {len(legacy)} (db count check: {db_count_check})")

# ---------- 3. Rule A: carry forward existing Phase4 result untouched ----------
existing_join_path = BASE / "legacy_mhlw_join.csv"
rule_a_pairs = {}  # clinic_id -> mhlw_facility_id
with open(existing_join_path, encoding="utf-8", newline="") as f:
    for row in csv.DictReader(f):
        if row["confidence"] == "NAME_ADDRESS" and row["candidate_count"] == "1":
            rule_a_pairs[row["clinic_id"]] = row["mhlw_facility_id_candidates"]
log(f"Rule A (carried forward from existing Phase4): {len(rule_a_pairs)}")

matched = {}  # clinic_id -> (mhlw_facility_id, match_method, confidence, name_mismatch)
for cid, fid in rule_a_pairs.items():
    matched[cid] = (fid, "NAME_ADDRESS_EXACT", "HIGH", False)

remaining_legacy_ids = set(legacy) - set(rule_a_pairs)
remaining_mhlw_ids = set(mhlw) - set(rule_a_pairs.values())
log(f"remaining after Rule A: legacy={len(remaining_legacy_ids)} mhlw={len(remaining_mhlw_ids)}")


def build_index(pool_ids, source, field):
    idx = defaultdict(list)
    for _id in pool_ids:
        key = source[_id][field]
        if key:
            idx[key].append(_id)
    return idx


def _type_ok(cid, fid):
    """医科/歯科の種別一致ガード。legacy側medical_typeが空の場合のみ不問(16件のみ該当)。

    休日応急診療所(医科)がMHLW側の休日応急歯科診療所(歯科)に誤マッチする等、
    住所・名称が近くても種別が異なる別施設をMATCH対象から確実に除外する。
    """
    mt = legacy[cid].get("medical_type", "")
    return not mt or mt == mhlw[fid]["facility_type"]


def apply_rule_single_key(remaining_legacy_ids, remaining_mhlw_ids, key_field, method, confidence, compute_mismatch=False):
    mhlw_idx = build_index(remaining_mhlw_ids, mhlw, key_field)
    legacy_idx = build_index(remaining_legacy_ids, legacy, key_field)
    newly_matched = {}
    for cid in list(remaining_legacy_ids):
        key = legacy[cid][key_field]
        if not key:
            continue
        mhlw_cands = [fid for fid in mhlw_idx.get(key, []) if _type_ok(cid, fid)]
        legacy_cands = legacy_idx.get(key, [])
        if len(mhlw_cands) == 1 and len(legacy_cands) == 1:
            fid = mhlw_cands[0]
            name_mismatch = False
            if compute_mismatch:
                name_mismatch = legacy[cid]["name_comparison"] != mhlw[fid]["name_comparison"]
            newly_matched[cid] = (fid, method, confidence, name_mismatch)
    for cid, (fid, *_rest) in newly_matched.items():
        remaining_legacy_ids.discard(cid)
        remaining_mhlw_ids.discard(fid)
    return newly_matched


def apply_rule_dual_key(remaining_legacy_ids, remaining_mhlw_ids, name_field, addr_field, method, confidence):
    mhlw_idx = defaultdict(list)
    for fid in remaining_mhlw_ids:
        key = (mhlw[fid][name_field], mhlw[fid][addr_field])
        if key[0] and key[1]:
            mhlw_idx[key].append(fid)
    legacy_idx = defaultdict(list)
    for cid in remaining_legacy_ids:
        key = (legacy[cid][name_field], legacy[cid][addr_field])
        if key[0] and key[1]:
            legacy_idx[key].append(cid)
    newly_matched = {}
    for cid in list(remaining_legacy_ids):
        key = (legacy[cid][name_field], legacy[cid][addr_field])
        if not key[0] or not key[1]:
            continue
        mhlw_cands = [fid for fid in mhlw_idx.get(key, []) if _type_ok(cid, fid)]
        legacy_cands = legacy_idx.get(key, [])
        if len(mhlw_cands) == 1 and len(legacy_cands) == 1:
            newly_matched[cid] = (mhlw_cands[0], method, confidence, False)
    for cid, (fid, *_rest) in newly_matched.items():
        remaining_legacy_ids.discard(cid)
        remaining_mhlw_ids.discard(fid)
    return newly_matched


# ---------- Rule B: light name + full address, 1:1 both sides ----------
rule_b = apply_rule_dual_key(remaining_legacy_ids, remaining_mhlw_ids, "name_light", "addr_full",
                              "NORMALIZED_NAME_ADDRESS", "HIGH")
matched.update(rule_b)
log(f"Rule B (NORMALIZED_NAME_ADDRESS): +{len(rule_b)}  remaining legacy={len(remaining_legacy_ids)}")

# ---------- Rule C: full address only, 1:1 both sides (name-agnostic) ----------
rule_c = apply_rule_single_key(remaining_legacy_ids, remaining_mhlw_ids, "addr_full",
                                "UNIQUE_ADDRESS_MATCH", "HIGH", compute_mismatch=True)
matched.update(rule_c)
log(f"Rule C (UNIQUE_ADDRESS_MATCH): +{len(rule_c)}  remaining legacy={len(remaining_legacy_ids)}")

# ---------- Rule D: base address + comparison name, 1:1 both sides ----------
rule_d = apply_rule_dual_key(remaining_legacy_ids, remaining_mhlw_ids, "name_comparison", "addr_base",
                              "BASE_ADDRESS_CORP_NAME", "HIGH")
matched.update(rule_d)
log(f"Rule D (BASE_ADDRESS_CORP_NAME): +{len(rule_d)}  remaining legacy={len(remaining_legacy_ids)}")

log(f"TOTAL MATCHED after A+B+C+D: {len(matched)}")

# ---------- diagnostics for still-unmatched clinics: REVIEW sub-classification ----------
final_remaining_mhlw = remaining_mhlw_ids  # facilities never claimed by any rule
full_idx_final = build_index(final_remaining_mhlw, mhlw, "addr_full")
base_idx_final = build_index(final_remaining_mhlw, mhlw, "addr_base")
name_idx_final = build_index(final_remaining_mhlw, mhlw, "name_comparison")

# also need to know original Phase4 classification (AMBIGUOUS_REVIEW/FUZZY_REVIEW/UNMATCHED) per clinic
orig_confidence = {}
with open(existing_join_path, encoding="utf-8", newline="") as f:
    for row in csv.DictReader(f):
        orig_confidence[row["clinic_id"]] = row["confidence"]

review_reason_counts = defaultdict(int)
review_reason_by_clinic = {}
for cid in remaining_legacy_ids:
    L = legacy[cid]
    f_match = full_idx_final.get(L["addr_full"], [])
    b_match = base_idx_final.get(L["addr_base"], [])
    n_match = name_idx_final.get(L["name_comparison"], [])
    orig = orig_confidence.get(cid, "UNMATCHED")
    if orig == "UNMATCHED":
        reason = "UNMATCHED"
    elif len(f_match) >= 2:
        reason = "同一住所に複数医院"
    elif len(b_match) >= 1 and len(n_match) >= 1 and len(f_match) == 0:
        reason = "住所正規化+法人表記差で一致(1:1でないため要確認)"
    elif len(b_match) >= 1 and len(n_match) == 0 and len(f_match) == 0:
        reason = "住所本体一致のみ・名称不一致"
    elif len(n_match) >= 1 and len(f_match) == 0 and len(b_match) == 0:
        reason = "名称のみ一致・住所別"
    elif len(f_match) >= 1 and len(n_match) == 0:
        reason = "住所のみ一致・名称差大"
    else:
        reason = "その他"
    review_reason_counts[reason] += 1
    review_reason_by_clinic[cid] = reason

log("REVIEW reason breakdown:")
for k, v in sorted(review_reason_counts.items(), key=lambda x: -x[1]):
    log(f"  {k}: {v}")

# ---------- write phase4_join_v2.csv (all 13,970 rows) ----------
fieldnames = [
    "clinic_id", "mhlw_facility_id",
    "clinic_name_raw", "mhlw_clinic_name_raw",
    "address_raw", "mhlw_address_raw",
    "normalized_clinic_name", "normalized_mhlw_name",
    "comparison_clinic_name", "comparison_mhlw_name",
    "normalized_full_address", "normalized_mhlw_full_address",
    "normalized_base_address", "normalized_mhlw_base_address",
    "join_status", "match_method", "match_confidence", "name_mismatch",
    "candidate_count_clinic", "candidate_count_mhlw", "review_reason",
]
legacy_full_addr_idx_all = build_index(set(legacy), legacy, "addr_full")  # 全13,970件基準の重複判定用(1回だけ構築)

rows_out = []
for cid, L in legacy.items():
    if cid in matched:
        fid, method, confidence, name_mismatch = matched[cid]
        M = mhlw[fid]
        rows_out.append({
            "clinic_id": cid, "mhlw_facility_id": fid,
            "clinic_name_raw": L["name_raw"], "mhlw_clinic_name_raw": M["name_raw"],
            "address_raw": L["addr_raw"], "mhlw_address_raw": M["addr_raw"],
            "normalized_clinic_name": L["name_light"], "normalized_mhlw_name": M["name_light"],
            "comparison_clinic_name": L["name_comparison"], "comparison_mhlw_name": M["name_comparison"],
            "normalized_full_address": L["addr_full"], "normalized_mhlw_full_address": M["addr_full"],
            "normalized_base_address": L["addr_base"], "normalized_mhlw_base_address": M["addr_base"],
            "join_status": "MATCHED", "match_method": method, "match_confidence": confidence,
            "name_mismatch": name_mismatch, "candidate_count_clinic": 1, "candidate_count_mhlw": 1,
            "review_reason": "",
        })
    else:
        f_match = full_idx_final.get(L["addr_full"], [])
        b_match = base_idx_final.get(L["addr_base"], [])
        reason = review_reason_by_clinic.get(cid, orig_confidence.get(cid, "UNMATCHED"))
        status = "UNMATCHED" if reason == "UNMATCHED" else "REVIEW"
        rows_out.append({
            "clinic_id": cid, "mhlw_facility_id": "",
            "clinic_name_raw": L["name_raw"], "mhlw_clinic_name_raw": "",
            "address_raw": L["addr_raw"], "mhlw_address_raw": "",
            "normalized_clinic_name": L["name_light"], "normalized_mhlw_name": "",
            "comparison_clinic_name": L["name_comparison"], "comparison_mhlw_name": "",
            "normalized_full_address": L["addr_full"], "normalized_mhlw_full_address": "",
            "normalized_base_address": L["addr_base"], "normalized_mhlw_base_address": "",
            "join_status": status, "match_method": "", "match_confidence": "", "name_mismatch": "",
            "candidate_count_clinic": len(legacy_full_addr_idx_all.get(L["addr_full"], [])),
            "candidate_count_mhlw": len(f_match),
            "review_reason": reason,
        })

with open(OUT_CSV, "w", encoding="utf-8", newline="") as out:
    w = csv.DictWriter(out, fieldnames=fieldnames)
    w.writeheader()
    w.writerows(rows_out)
log(f"wrote {OUT_CSV} ({len(rows_out)} rows)")

# ---------- summary ----------
method_counts = defaultdict(int)
for cid, (fid, method, confidence, name_mismatch) in matched.items():
    method_counts[method] += 1

old_matched = len(rule_a_pairs)
new_matched = len(matched)
added = new_matched - old_matched
total = len(legacy)
review_count = sum(1 for cid in remaining_legacy_ids if review_reason_by_clinic.get(cid) != "UNMATCHED")
unmatched_count = sum(1 for cid in remaining_legacy_ids if review_reason_by_clinic.get(cid) == "UNMATCHED")

summary = {
    "legacy_total": total,
    "old_matched": old_matched,
    "new_matched": new_matched,
    "added_matched": added,
    "new_matched_rate": round(new_matched / total, 4),
    "old_matched_rate": round(old_matched / total, 4),
    "review_remaining": review_count,
    "unmatched": unmatched_count,
    "match_method_breakdown": dict(method_counts),
    "review_reason_breakdown": dict(review_reason_counts),
    "note": "Rule A = 既存Phase4結果をそのまま踏襲(再計算なし)。Production DB/既存sidecar/既存Phase4成果物は無変更。",
}
with open(OUT_JSON, "w", encoding="utf-8") as out:
    json.dump(summary, out, ensure_ascii=False, indent=2)
log(json.dumps(summary, ensure_ascii=False, indent=2))

# ---------- unique_address_match_audit.csv (Rule C promotions) ----------
audit_rows = []
for cid, (fid, method, confidence, name_mismatch) in matched.items():
    if method != "UNIQUE_ADDRESS_MATCH":
        continue
    L, M = legacy[cid], mhlw[fid]
    from difflib import SequenceMatcher
    sim = round(SequenceMatcher(None, L["name_comparison"], M["name_comparison"]).ratio(), 3)
    audit_rows.append({
        "clinic_id": cid, "clinic_master_name": L["name_raw"], "mhlw_name": M["name_raw"],
        "clinic_master_address": L["addr_raw"], "mhlw_address": M["addr_raw"],
        "normalized_address": L["addr_full"], "name_similarity": sim, "match_method": method,
        "name_mismatch": name_mismatch,
    })
audit_rows.sort(key=lambda r: r["name_similarity"])  # most divergent names first, for review usefulness
with open(OUT_AUDIT, "w", encoding="utf-8", newline="") as out:
    fn = ["clinic_id", "clinic_master_name", "mhlw_name", "clinic_master_address", "mhlw_address",
          "normalized_address", "name_similarity", "match_method", "name_mismatch"]
    w = csv.DictWriter(out, fieldnames=fn)
    w.writeheader()
    w.writerows(audit_rows)
log(f"UNIQUE_ADDRESS_MATCH total: {len(audit_rows)}, wrote all to {OUT_AUDIT}")

# ---------- v2 sidecar DB (separate from existing) ----------
OUT_SIDECAR.unlink(missing_ok=True)
for suffix in ("-wal", "-shm"):
    Path(str(OUT_SIDECAR) + suffix).unlink(missing_ok=True)
sconn = sqlite3.connect(OUT_SIDECAR)
sconn.executescript("""
CREATE TABLE clinic_mhlw_join_v2(
 clinic_id INTEGER PRIMARY KEY, mhlw_facility_id TEXT NOT NULL DEFAULT '',
 join_status TEXT NOT NULL, match_method TEXT NOT NULL DEFAULT '',
 match_confidence TEXT NOT NULL DEFAULT '', name_mismatch INTEGER NOT NULL DEFAULT 0,
 review_reason TEXT NOT NULL DEFAULT '');
""")
for r in rows_out:
    sconn.execute("INSERT INTO clinic_mhlw_join_v2 VALUES(?,?,?,?,?,?,?)", (
        int(r["clinic_id"]), r["mhlw_facility_id"], r["join_status"], r["match_method"],
        r["match_confidence"], int(bool(r["name_mismatch"])) if r["name_mismatch"] != "" else 0,
        r["review_reason"]))
sconn.commit()
sconn.close()
log(f"wrote {OUT_SIDECAR}")

# ---------- final DB protection check ----------
import hashlib
h = hashlib.sha256(DB_PATH.read_bytes()).hexdigest()
log(f"Production DB SHA-256 after run: {h}")
