"""Phase 2: MHLW department master (READ ONLY dry run).

Reads the 4 fixed MHLW CSVs and produces mhlw_department_master.csv.
Does NOT touch ./data/clinics.sqlite3. Pure local file -> local file.
"""
import csv
import sys
from paths import BASE, source

csv.field_size_limit(sys.maxsize)

FACILITY_FILES = [
    ("医科", source("02-1_clinic_facility_info_20260601.csv"), "02-1_clinic_facility_info_20260601.csv"),
    ("歯科", source("03-1_dental_facility_info_20260601.csv"), "03-1_dental_facility_info_20260601.csv"),
]
SPECIALITY_FILES = [
    ("医科", source("02-2_clinic_speciality_hours_20260601.csv"), "02-2_clinic_speciality_hours_20260601.csv"),
    ("歯科", source("03-2_dental_speciality_hours_20260601.csv"), "03-2_dental_speciality_hours_20260601.csv"),
]
SOURCE_DATE = "2026-06-01"  # from filename suffix 20260601
OUT_PATH = BASE / "mhlw_department_master.csv"

facility_index = {}  # (facility_type, id) -> dict
for facility_type, path, fname in FACILITY_FILES:
    with open(path, encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            facility_index[(facility_type, row["ID"])] = {
                "clinic_name": row["正式名称"],
                "address": row["所在地"],
                "postal_code": "",  # NOT PRESENT in MHLW source CSV (confirmed absent in Phase 1 audit)
                "phone": "",        # NOT PRESENT in MHLW source CSV (confirmed absent in Phase 1 audit)
            }
print(f"facility_index built: {len(facility_index)} facilities (医科+歯科)", file=sys.stderr)

seen_dedup = set()  # (facility_type, id, dept_code, dept_name) dedup ONLY on this derived master, per instruction
n_written = 0
n_dropped_no_facility = 0
n_dedup_skipped = 0

with open(OUT_PATH, "w", encoding="utf-8", newline="") as out:
    w = csv.writer(out)
    w.writerow(["mhlw_facility_id", "facility_type", "clinic_name", "postal_code", "address", "phone",
                "department_code", "department_name", "source_date", "source_file"])
    for facility_type, path, fname in SPECIALITY_FILES:
        with open(path, encoding="utf-8-sig", newline="") as f:
            r = csv.DictReader(f)
            for row in r:
                fid = row["ID"]
                key = (facility_type, fid, row["診療科目コード"], row["診療科目名"])
                if key in seen_dedup:
                    n_dedup_skipped += 1
                    continue
                seen_dedup.add(key)
                fac = facility_index.get((facility_type, fid))
                if fac is None:
                    n_dropped_no_facility += 1
                    continue
                w.writerow([fid, facility_type, fac["clinic_name"], fac["postal_code"], fac["address"], fac["phone"],
                            row["診療科目コード"], row["診療科目名"], SOURCE_DATE, fname])
                n_written += 1

print(f"rows written: {n_written}", file=sys.stderr)
print(f"dedup-skipped (same ID+code+name repeated): {n_dedup_skipped}", file=sys.stderr)
print(f"dropped (speciality row with no matching facility ID): {n_dropped_no_facility}", file=sys.stderr)
