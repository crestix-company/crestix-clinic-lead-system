"""安全MATCHED集合のMHLW正式診療科/Crestix営業カテゴリ集計。DBはREAD ONLY。"""
import csv
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent
SAFE_METHODS = {"NAME_ADDRESS_EXACT", "NORMALIZED_NAME_ADDRESS", "BASE_ADDRESS_CORP_NAME"}
PROMOTABLE = {"HP_IDENTITY_CONFIRMED", "HP_RENAME_CONFIRMED"}


def load_sets():
    joins = list(csv.DictReader(open(BASE / "phase4_join_v2.csv", encoding="utf-8", newline="")))
    safe = {r["clinic_id"]: r["mhlw_facility_id"] for r in joins if r["match_method"] in SAFE_METHODS}
    audit = list(csv.DictReader(open(BASE / "rule_c_hp_identity_audit.csv", encoding="utf-8", newline="")))
    promoted = {r["clinic_id"]: r["mhlw_facility_id"] for r in audit if r["identity_status"] in PROMOTABLE}
    return safe, {**safe, **promoted}


def aggregate(matches, departments, mapping):
    by_facility = defaultdict(list)
    for d in departments:
        by_facility[d["mhlw_facility_id"]].append(d)
    counts = []
    official_names = Counter()
    official_codes = set()
    crestix_clinics = defaultdict(set)
    records = []
    for cid, fid in matches.items():
        rows = by_facility.get(fid, [])
        counts.append(len(rows))
        for d in rows:
            code, name = d["department_code"], d["department_name"]
            status, crestix = mapping.get((code, name), ("UNMAPPED", ""))
            crestix = crestix if status in {"EXACT", "ALIAS"} else ""
            official_codes.add(code)
            official_names[name] += 1
            if crestix:
                crestix_clinics[crestix].add(cid)
            records.append({"clinic_id": cid, "mhlw_facility_id": fid,
                "mhlw_department_code": code, "mhlw_department_name": name,
                "crestix_department": crestix})
    return {
        "matched_clinics": len(matches),
        "clinics_with_departments": sum(bool(n) for n in counts),
        "clinics_with_zero_departments": sum(not n for n in counts),
        "department_record_count": sum(counts),
        "unique_department_code_count": len(official_codes),
        "unique_department_name_count": len(official_names),
        "average_departments_per_clinic": round(statistics.mean(counts), 3),
        "median_departments_per_clinic": statistics.median(counts),
        "max_departments_per_clinic": max(counts),
        "selected_official_name_clinic_counts": {
            name: len({r["clinic_id"] for r in records if r["mhlw_department_name"] == name})
            for name in ("内科", "心療内科", "循環器内科", "消化器内科", "糖尿病内科")},
        "crestix_sales_category_clinic_counts": {k: len(v) for k, v in sorted(crestix_clinics.items())},
    }, records


def main():
    safe, final = load_sets()
    departments = list(csv.DictReader(open(BASE / "mhlw_department_master.csv", encoding="utf-8", newline="")))
    mapping_rows = csv.DictReader(open(BASE / "mhlw_to_crestix_department_mapping.csv", encoding="utf-8", newline=""))
    mapping = {(r["department_code"], r["department_name"]): (r["status"], r["crestix_department"]) for r in mapping_rows}
    before, _ = aggregate(safe, departments, mapping)
    after, records = aggregate(final, departments, mapping)
    summary = {"safe_matched_before_hp": before, "final_matched_after_hp": after,
               "hp_promoted": len(final) - len(safe)}
    (BASE / "safe_matched_department_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with open(BASE / "final_matched_department_records.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["clinic_id", "mhlw_facility_id", "mhlw_department_code",
            "mhlw_department_name", "crestix_department"])
        w.writeheader()
        w.writerows(records)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
