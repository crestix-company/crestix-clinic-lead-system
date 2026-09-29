"""HP昇格候補の最終監査と本番切替前final sidecar生成（Production DB非更新）。"""
import csv
import json
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mhlw_dry_run.summarize_safe_matched import SAFE_METHODS
from src.normalizer.address import normalize_address
from src.normalizer.phone import normalize_phone

BASE = Path(__file__).resolve().parent
AUDIT_OUT = BASE / "final_hp_promotion_audit.csv"
SIDECAR_OUT = BASE / "clinic_mhlw_departments_final.sqlite3"
SUMMARY_OUT = BASE / "final_mhlw_join_summary.json"
PROMOTION_STATUSES = {"HP_IDENTITY_CONFIRMED", "HP_RENAME_CONFIRMED"}


def compatible_address(expected, observed):
    expected, observed = normalize_address(expected), normalize_address(observed)
    return bool(observed and (expected in observed or observed in expected))


def audit_promotions(rows):
    audited = []
    for r in rows:
        if r["identity_status"] not in PROMOTION_STATUSES:
            continue
        address_ok = compatible_address(r["clinic_address"], r["hp_address"]) and compatible_address(r["mhlw_address"], r["hp_address"])
        clinic_phone, hp_phone = normalize_phone(r["clinic_phone"]), normalize_phone(r["hp_phone"])
        phone_ok = bool(clinic_phone and hp_phone and clinic_phone == hp_phone)
        allowed = address_ok and phone_ok
        failed = []
        if not address_ok:
            failed.append("HP_EXTRACTED_ADDRESS_NOT_FACILITY_SPECIFIC_OR_MISMATCH")
        if not phone_ok:
            failed.append("HP_EXTRACTED_PHONE_MISMATCH_OR_NOT_FACILITY_SPECIFIC")
        reason = "FACILITY_SPECIFIC_HP_ADDRESS_AND_PHONE_CONFIRMED" if allowed else ";".join(failed)
        audited.append({
            "clinic_id": r["clinic_id"], "mhlw_facility_id": r["mhlw_facility_id"],
            "clinic_name": r["clinic_name"], "mhlw_clinic_name": r["mhlw_clinic_name"], "hp_name": r["hp_name"],
            "clinic_address": r["clinic_address"], "mhlw_address": r["mhlw_address"], "hp_address": r["hp_address"],
            "clinic_phone": r["clinic_phone"], "mhlw_phone": r["mhlw_phone"], "hp_phone": r["hp_phone"],
            "selected_identity_url": r["selected_identity_url"], "final_url": r["final_url"], "final_domain": r["final_domain"],
            "previous_identity_status": r["identity_status"],
            "final_identity_status": r["identity_status"] if allowed else "HP_CONFLICT_REVIEW",
            "promotion_allowed": str(allowed).lower(), "audit_reason": reason,
        })
    return audited


def aggregate(final_matches, departments, mapping):
    by_facility = defaultdict(list)
    for d in departments:
        by_facility[d["mhlw_facility_id"]].append(d)
    counts, names, codes, categories = [], Counter(), set(), defaultdict(set)
    for cid, meta in final_matches.items():
        rows = by_facility.get(meta["mhlw_facility_id"], [])
        counts.append(len(rows))
        for d in rows:
            code, name = d["department_code"], d["department_name"]
            codes.add(code); names[name] += 1
            status, category = mapping.get((code, name), ("UNMAPPED", ""))
            if status in {"EXACT", "ALIAS"} and category:
                categories[category].add(cid)
    return {
        "matched_clinics": len(final_matches), "clinics_with_departments": sum(bool(x) for x in counts),
        "clinics_with_zero_departments": sum(not x for x in counts), "department_record_count": sum(counts),
        "unique_department_code_count": len(codes), "unique_department_name_count": len(names),
        "average_departments_per_clinic": round(statistics.mean(counts), 3),
        "median_departments_per_clinic": statistics.median(counts), "max_departments_per_clinic": max(counts),
        "selected_official_name_clinic_counts": {n: sum(1 for cid, m in final_matches.items()
            if any(d["department_name"] == n for d in by_facility.get(m["mhlw_facility_id"], [])))
            for n in ("内科", "心療内科", "循環器内科", "消化器内科", "糖尿病内科")},
        "crestix_sales_category_clinic_counts": {k: len(v) for k, v in sorted(categories.items())},
    }


def main():
    joins = list(csv.DictReader(open(BASE / "phase4_join_v2.csv", encoding="utf-8", newline="")))
    hp_rows = list(csv.DictReader(open(BASE / "rule_c_hp_identity_audit.csv", encoding="utf-8", newline="")))
    audited = audit_promotions(hp_rows)
    fields = ["clinic_id", "mhlw_facility_id", "clinic_name", "mhlw_clinic_name", "hp_name",
        "clinic_address", "mhlw_address", "hp_address", "clinic_phone", "mhlw_phone", "hp_phone",
        "selected_identity_url", "final_url", "final_domain", "previous_identity_status",
        "final_identity_status", "promotion_allowed", "audit_reason"]
    with open(AUDIT_OUT, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(audited)

    allowed = {r["clinic_id"]: r for r in audited if r["promotion_allowed"] == "true"}
    final_matches = {}
    for r in joins:
        if r["match_method"] in SAFE_METHODS:
            final_matches[r["clinic_id"]] = {"mhlw_facility_id": r["mhlw_facility_id"],
                "match_method": r["match_method"], "match_confidence": r["match_confidence"], "identity_status": "NOT_REQUIRED"}
    for cid, r in allowed.items():
        final_matches[cid] = {"mhlw_facility_id": r["mhlw_facility_id"], "match_method": "UNIQUE_ADDRESS_MATCH",
            "match_confidence": "HP_VERIFIED", "identity_status": r["final_identity_status"]}

    departments = list(csv.DictReader(open(BASE / "mhlw_department_master.csv", encoding="utf-8", newline="")))
    mapping_rows = csv.DictReader(open(BASE / "mhlw_to_crestix_department_mapping.csv", encoding="utf-8", newline=""))
    mapping = {(r["department_code"], r["department_name"]): (r["status"], r["crestix_department"]) for r in mapping_rows}
    by_facility = defaultdict(list)
    for d in departments: by_facility[d["mhlw_facility_id"]].append(d)

    SIDECAR_OUT.unlink(missing_ok=True)
    conn = sqlite3.connect(SIDECAR_OUT)
    conn.executescript("""
    CREATE TABLE clinic_mhlw_departments_final(
      clinic_id INTEGER NOT NULL, mhlw_facility_id TEXT NOT NULL,
      mhlw_department_code TEXT NOT NULL, mhlw_department_name TEXT NOT NULL,
      crestix_department TEXT NOT NULL DEFAULT '', match_method TEXT NOT NULL,
      match_confidence TEXT NOT NULL, identity_status TEXT NOT NULL, source_date TEXT NOT NULL,
      mapping_status TEXT NOT NULL,
      PRIMARY KEY(clinic_id,mhlw_department_code,mhlw_department_name));
    CREATE INDEX idx_final_crestix ON clinic_mhlw_departments_final(crestix_department,clinic_id);
    CREATE INDEX idx_final_mhlw_name ON clinic_mhlw_departments_final(mhlw_department_name,clinic_id);
    CREATE VIEW clinic_mhlw_departments AS
      SELECT clinic_id,mhlw_facility_id,mhlw_department_code AS department_code,
             mhlw_department_name AS department_name,crestix_department,mapping_status,source_date
      FROM clinic_mhlw_departments_final;
    """)
    for cid, meta in final_matches.items():
        for d in by_facility.get(meta["mhlw_facility_id"], []):
            status, category = mapping.get((d["department_code"], d["department_name"]), ("UNMAPPED", ""))
            if status not in {"EXACT", "ALIAS"}: category = ""
            conn.execute("INSERT INTO clinic_mhlw_departments_final VALUES(?,?,?,?,?,?,?,?,?,?)", (
                int(cid), meta["mhlw_facility_id"], d["department_code"], d["department_name"], category,
                meta["match_method"], meta["match_confidence"], meta["identity_status"], d["source_date"], status))
    conn.commit(); conn.close()

    hp_by_cid = {r["clinic_id"]: r for r in hp_rows}
    final_audit_by_cid = {r["clinic_id"]: r for r in audited}
    remaining = Counter()
    for r in joins:
        if r["clinic_id"] in final_matches: continue
        if r["match_method"] == "UNIQUE_ADDRESS_MATCH":
            final_audit = final_audit_by_cid.get(r["clinic_id"])
            remaining[(final_audit or hp_by_cid[r["clinic_id"]])["final_identity_status" if final_audit else "identity_status"]] += 1
        elif r["join_status"] == "UNMATCHED": remaining["UNMATCHED"] += 1
        elif "1:1でない" in r["review_reason"] or "同一住所" in r["review_reason"]: remaining["AMBIGUOUS"] += 1
        else: remaining["FUZZY_REVIEW"] += 1

    summary = {"promotion_candidates": len(audited), "promotion_allowed": len(allowed),
        "promotion_returned_to_review": len(audited) - len(allowed), "final_matched": len(final_matches),
        "final_matched_rate": round(len(final_matches) / len(joins), 6),
        "department_summary": aggregate(final_matches, departments, mapping),
        "remaining_total": len(joins) - len(final_matches), "remaining_reason_counts": dict(sorted(remaining.items()))}
    SUMMARY_OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__": main()
