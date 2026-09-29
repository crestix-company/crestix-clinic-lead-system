"""Phase 3: MHLW department code/name -> Crestix department mapping (READ ONLY dry run).

Mapping RULES live in crestix_department_mapping.py (pure, no file I/O, Git-tracked,
covered by tests/test_mhlw_crestix_mapping.py). This script only reads the MHLW CSVs to
enumerate every unique (code,name) pair actually present in the data, classifies each via
crestix_department_mapping.classify(), and writes the two CSV deliverables.

Requires the 2 MHLW speciality CSVs (see README.md "必要なMHLW CSV"). Run directly:
    python3 mhlw_dry_run/phase3_build_crestix_mapping.py
"""
import csv
import sys
from collections import Counter
from pathlib import Path

csv.field_size_limit(sys.maxsize)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from crestix_department_mapping import classify  # noqa: E402

SPECIALITY_FILES = [
    "/Users/maekawahiroyuki/Downloads/02-2_clinic_speciality_hours_20260601.csv",
    "/Users/maekawahiroyuki/Downloads/03-2_dental_speciality_hours_20260601.csv",
]
MAP_OUT = Path(__file__).resolve().parent / "mhlw_to_crestix_department_mapping.csv"
REVIEW_OUT = Path(__file__).resolve().parent / "department_mapping_review.csv"


def main():
    counter = Counter()
    for path in SPECIALITY_FILES:
        with open(path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                counter[(row["診療科目コード"], row["診療科目名"])] += 1

    print(f"unique (code,name) pairs: {len(counter)}", file=sys.stderr)

    rows = []
    for (code, name), cnt in sorted(counter.items()):
        status, target, reason = classify(code, name)
        rows.append({
            "department_code": code, "department_name": name, "occurrence_count": cnt,
            "status": status, "crestix_department": target, "reason": reason,
        })

    with open(MAP_OUT, "w", encoding="utf-8", newline="") as out:
        w = csv.DictWriter(out, fieldnames=["department_code", "department_name", "occurrence_count", "status", "crestix_department", "reason"])
        w.writeheader()
        w.writerows(rows)

    review_rows = [r for r in rows if r["status"] == "REVIEW"]
    review_rows.sort(key=lambda r: -r["occurrence_count"])
    with open(REVIEW_OUT, "w", encoding="utf-8", newline="") as out:
        w = csv.DictWriter(out, fieldnames=["department_code", "department_name", "occurrence_count", "crestix_department", "reason"])
        w.writeheader()
        for r in review_rows:
            w.writerow({k: r[k] for k in ["department_code", "department_name", "occurrence_count", "crestix_department", "reason"]})

    status_counts = Counter(r["status"] for r in rows)
    print("status breakdown (unique code/name pairs):", dict(status_counts), file=sys.stderr)
    weighted = Counter()
    for r in rows:
        weighted[r["status"]] += r["occurrence_count"]
    print("status breakdown (occurrence-weighted):", dict(weighted), file=sys.stderr)
    print(f"REVIEW rows: {len(review_rows)}", file=sys.stderr)


if __name__ == "__main__":
    main()
