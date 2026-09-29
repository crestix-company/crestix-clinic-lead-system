"""Build the MHLW verification sidecar SQLite DB from Phase 2-6.5 CSV deliverables.

Does NOT touch ./data/clinics.sqlite3. Produces a brand-new, independent file:
  mhlw_dry_run/clinic_mhlw_departments.sqlite3

clinic_id here is the EXISTING clinics.id (legacy inner ID) taken as-is from
legacy_mhlw_join.csv. No new internal ID is minted anywhere in this pipeline.

Tables:
  clinic_mhlw_departments   -- department-level rows, ONLY for NAME_ADDRESS 1:1
                               confirmed clinics, ONLY EXACT/ALIAS (=official) mappings.
                               This is what the sales filter is allowed to use.
  clinic_mhlw_join_status   -- ALL 13,970 legacy clinics, one row each, with their
                               join confidence (NAME_ADDRESS/AMBIGUOUS_REVIEW/
                               FUZZY_REVIEW/UNMATCHED). Info/audit only - never used
                               as a sales filter condition.
"""
import csv
import sqlite3
import sys
from pathlib import Path

csv.field_size_limit(sys.maxsize)

BASE = Path("/Users/maekawahiroyuki/Desktop/clinic-list-filter-complete/mhlw_dry_run")
OUT_DB = BASE / "clinic_mhlw_departments.sqlite3"
CRESTIX_TARGETS = {"消化器内科", "眼科", "糖尿病内科", "泌尿器科", "循環器内科", "皮膚科", "歯科", "美容整形外科", "産婦人科"}

OUT_DB.unlink(missing_ok=True)
for suffix in ("-wal", "-shm"):
    Path(str(OUT_DB) + suffix).unlink(missing_ok=True)

conn = sqlite3.connect(OUT_DB)
conn.executescript("""
CREATE TABLE clinic_mhlw_departments(
 clinic_id INTEGER NOT NULL,
 mhlw_facility_id TEXT NOT NULL,
 mhlw_department_code TEXT NOT NULL,
 mhlw_department_name TEXT NOT NULL,
 crestix_department TEXT NOT NULL,
 mapping_status TEXT NOT NULL,
 source_date TEXT NOT NULL
);
CREATE INDEX idx_cmd_clinic ON clinic_mhlw_departments(clinic_id);
CREATE INDEX idx_cmd_dept ON clinic_mhlw_departments(crestix_department);

CREATE TABLE clinic_mhlw_join_status(
 clinic_id INTEGER PRIMARY KEY,
 confidence TEXT NOT NULL,
 mhlw_facility_id_candidates TEXT NOT NULL DEFAULT ''
);
""")

# ---- join status: ALL legacy clinics (info/audit only) ----
join_rows = []
confirmed_facility = {}  # clinic_id -> mhlw_facility_id (only NAME_ADDRESS 1:1)
with open(BASE / "legacy_mhlw_join.csv", encoding="utf-8", newline="") as f:
    for row in csv.DictReader(f):
        join_rows.append((int(row["clinic_id"]), row["confidence"], row["mhlw_facility_id_candidates"]))
        if row["confidence"] == "NAME_ADDRESS" and row["candidate_count"] == "1":
            confirmed_facility[row["clinic_id"]] = row["mhlw_facility_id_candidates"]
conn.executemany("INSERT INTO clinic_mhlw_join_status VALUES(?,?,?)", join_rows)
print(f"join_status rows inserted: {len(join_rows)}", file=sys.stderr)
print(f"NAME_ADDRESS confirmed clinics: {len(confirmed_facility)}", file=sys.stderr)

# ---- department code -> (status, crestix_department), EXACT/ALIAS only ----
code_to_target = {}
with open(BASE / "mhlw_to_crestix_department_mapping.csv", encoding="utf-8", newline="") as f:
    for row in csv.DictReader(f):
        if row["status"] in ("EXACT", "ALIAS") and row["crestix_department"] in CRESTIX_TARGETS:
            code_to_target[row["department_code"]] = (row["status"], row["crestix_department"])

# ---- facility_id -> list of (code, name, status, target) EXACT/ALIAS rows ----
facility_depts = {}
with open(BASE / "mhlw_department_master.csv", encoding="utf-8", newline="") as f:
    for row in csv.DictReader(f):
        hit = code_to_target.get(row["department_code"])
        if not hit:
            continue
        status, target = hit
        facility_depts.setdefault(row["mhlw_facility_id"], []).append(
            (row["department_code"], row["department_name"], status, target, row["source_date"]))

dept_rows = []
for clinic_id, fid in confirmed_facility.items():
    for code, name, status, target, source_date in facility_depts.get(fid, ()):
        dept_rows.append((int(clinic_id), fid, code, name, target, status, source_date))

conn.executemany("INSERT INTO clinic_mhlw_departments VALUES(?,?,?,?,?,?,?)", dept_rows)
conn.commit()
print(f"clinic_mhlw_departments rows inserted: {len(dept_rows)}", file=sys.stderr)
print(f"unique clinics with >=1 official Crestix dept: {len({r[0] for r in dept_rows})}", file=sys.stderr)
conn.close()
print(f"sidecar DB written: {OUT_DB}", file=sys.stderr)
