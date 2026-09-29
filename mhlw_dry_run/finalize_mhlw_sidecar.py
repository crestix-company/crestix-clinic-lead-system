"""Build the audited MHLW sidecar without network access or mutable HP evidence."""
import argparse
import csv
import hashlib
import json
import os
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mhlw_dry_run.summarize_safe_matched import SAFE_METHODS
from mhlw_dry_run.paths import SOURCE_DIR

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
DECISIONS_PATH = BASE / "final_hp_promotion_decisions.csv"
MANIFEST_PATH = BASE / "mhlw_source_manifest.json"
SIDECAR_OUT = BASE / "clinic_mhlw_departments_final.sqlite3"
SUMMARY_OUT = BASE / "final_mhlw_join_summary.json"
PROMOTION_STATUSES = {"HP_IDENTITY_CONFIRMED", "HP_RENAME_CONFIRMED"}
EXPECTED_PROMOTIONS = {"HP_IDENTITY_CONFIRMED": 3, "HP_RENAME_CONFIRMED": 52}


class DeterministicBuildError(RuntimeError):
    """A fail-closed validation error in the deterministic sidecar build."""


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_source_manifest(manifest_path=MANIFEST_PATH, source_dir=None, clinic_db=None):
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source_dir = Path(source_dir) if source_dir else SOURCE_DIR
    clinic_db = Path(clinic_db) if clinic_db else ROOT / manifest["clinic_master"]["path"]
    problems = []
    for expected in manifest["files"]:
        path = source_dir / expected["filename"]
        if not path.is_file():
            problems.append(f"missing: {path}")
            continue
        actual_size, actual_sha = path.stat().st_size, sha256_file(path)
        if actual_size != expected["size"] or actual_sha != expected["sha256"]:
            problems.append(
                f"{expected['filename']}: expected size={expected['size']} sha256={expected['sha256']}; "
                f"got size={actual_size} sha256={actual_sha}"
            )
    db_expected = manifest["clinic_master"]
    if not clinic_db.is_file():
        problems.append(f"missing Clinic Master: {clinic_db}")
    else:
        actual_size, actual_sha = clinic_db.stat().st_size, sha256_file(clinic_db)
        if actual_size != db_expected["size"] or actual_sha != db_expected["sha256"]:
            problems.append(
                f"Clinic Master snapshot mismatch: expected size={db_expected['size']} "
                f"sha256={db_expected['sha256']}; got size={actual_size} sha256={actual_sha}"
            )
        with sqlite3.connect(f"file:{clinic_db}?mode=ro", uri=True) as conn:
            count = conn.execute("SELECT count(*) FROM clinics").fetchone()[0]
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if count != db_expected["count"] or integrity != "ok":
            problems.append(
                f"Clinic Master validation failed: expected count={db_expected['count']} integrity=ok; "
                f"got count={count} integrity={integrity}"
            )
    if problems:
        raise DeterministicBuildError("MHLW source fingerprint mismatch\n" + "\n".join(problems))
    return manifest


def load_clinic_ids(clinic_db):
    with sqlite3.connect(f"file:{Path(clinic_db)}?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        return {str(row[0]) for row in conn.execute("SELECT id FROM clinics")}


def load_and_validate_decisions(path, clinic_ids, mhlw_facility_ids):
    with Path(path).open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    required = {
        "decision_schema_version", "clinic_id", "mhlw_facility_id", "final_identity_status",
        "promotion_allowed", "audit_reason", "match_method", "decision_source", "source_date",
    }
    if not rows or not required.issubset(rows[0]):
        raise DeterministicBuildError(f"promotion decision schema is invalid; required={sorted(required)}")
    clinic_keys = [row["clinic_id"] for row in rows]
    pair_keys = [(row["clinic_id"], row["mhlw_facility_id"]) for row in rows]
    if len(clinic_keys) != len(set(clinic_keys)):
        raise DeterministicBuildError("duplicate clinic_id in promotion decisions")
    if len(pair_keys) != len(set(pair_keys)):
        raise DeterministicBuildError("duplicate clinic_id/mhlw_facility_id in promotion decisions")
    if any(row["decision_schema_version"] != "1" for row in rows):
        raise DeterministicBuildError("unsupported promotion decision schema version")
    invalid_bools = {row["promotion_allowed"] for row in rows} - {"true", "false"}
    if invalid_bools:
        raise DeterministicBuildError(f"invalid promotion_allowed values: {sorted(invalid_bools)}")
    unknown_clinics = sorted(set(clinic_keys) - set(clinic_ids))
    if unknown_clinics:
        raise DeterministicBuildError(f"unknown clinic_id in promotion decisions: {unknown_clinics[:10]}")
    unknown_facilities = sorted({row["mhlw_facility_id"] for row in rows} - set(mhlw_facility_ids))
    if unknown_facilities:
        raise DeterministicBuildError(f"unknown mhlw_facility_id in promotion decisions: {unknown_facilities[:10]}")
    promoted = [row for row in rows if row["promotion_allowed"] == "true"]
    status_counts = Counter(row["final_identity_status"] for row in promoted)
    if len(promoted) != 55 or status_counts != Counter(EXPECTED_PROMOTIONS):
        raise DeterministicBuildError(
            f"promotion decision count mismatch: total={len(promoted)} statuses={dict(status_counts)}"
        )
    if any(row["final_identity_status"] not in PROMOTION_STATUSES for row in promoted):
        raise DeterministicBuildError("a non-confirmed decision is marked promotion_allowed=true")
    return rows


def aggregate(final_matches, departments, mapping):
    by_facility = defaultdict(list)
    for department in departments:
        by_facility[department["mhlw_facility_id"]].append(department)
    counts, names, codes, categories = [], Counter(), set(), defaultdict(set)
    for clinic_id, meta in final_matches.items():
        rows = by_facility.get(meta["mhlw_facility_id"], [])
        counts.append(len(rows))
        for department in rows:
            code, name = department["department_code"], department["department_name"]
            codes.add(code)
            names[name] += 1
            status, category = mapping.get((code, name), ("UNMAPPED", ""))
            if status in {"EXACT", "ALIAS"} and category:
                categories[category].add(clinic_id)
    return {
        "matched_clinics": len(final_matches),
        "clinics_with_departments": sum(bool(value) for value in counts),
        "clinics_with_zero_departments": sum(not value for value in counts),
        "department_record_count": sum(counts),
        "unique_department_code_count": len(codes),
        "unique_department_name_count": len(names),
        "average_departments_per_clinic": round(statistics.mean(counts), 3),
        "median_departments_per_clinic": statistics.median(counts),
        "max_departments_per_clinic": max(counts),
        "selected_official_name_clinic_counts": {
            name: sum(
                1 for meta in final_matches.values()
                if any(d["department_name"] == name for d in by_facility.get(meta["mhlw_facility_id"], []))
            )
            for name in ("内科", "心療内科", "循環器内科", "消化器内科", "糖尿病内科")
        },
        "crestix_sales_category_clinic_counts": {key: len(value) for key, value in sorted(categories.items())},
    }


def build_sidecar(joins_path, departments_path, mapping_path, decisions_path, clinic_db, output_path, summary_path=None):
    with Path(joins_path).open(encoding="utf-8", newline="") as stream:
        joins = list(csv.DictReader(stream))
    with Path(departments_path).open(encoding="utf-8", newline="") as stream:
        departments = list(csv.DictReader(stream))
    mhlw_facility_ids = {row["mhlw_facility_id"] for row in departments}
    decisions = load_and_validate_decisions(decisions_path, load_clinic_ids(clinic_db), mhlw_facility_ids)
    allowed = {row["clinic_id"]: row for row in decisions if row["promotion_allowed"] == "true"}

    final_matches = {}
    for row in joins:
        if row["match_method"] in SAFE_METHODS:
            final_matches[row["clinic_id"]] = {
                "mhlw_facility_id": row["mhlw_facility_id"], "match_method": row["match_method"],
                "match_confidence": row["match_confidence"], "identity_status": "NOT_REQUIRED",
            }
    for clinic_id, row in allowed.items():
        final_matches[clinic_id] = {
            "mhlw_facility_id": row["mhlw_facility_id"], "match_method": row["match_method"],
            "match_confidence": "HP_VERIFIED", "identity_status": row["final_identity_status"],
        }

    with Path(mapping_path).open(encoding="utf-8", newline="") as stream:
        mapping = {
            (row["department_code"], row["department_name"]): (row["status"], row["crestix_department"])
            for row in csv.DictReader(stream)
        }
    by_facility = defaultdict(list)
    for department in departments:
        by_facility[department["mhlw_facility_id"]].append(department)

    output_path = Path(output_path)
    temporary_path = output_path.with_name(output_path.name + ".building")
    temporary_path.unlink(missing_ok=True)
    conn = sqlite3.connect(temporary_path)
    try:
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
        for clinic_id in sorted(final_matches, key=int):
            meta = final_matches[clinic_id]
            rows = sorted(by_facility.get(meta["mhlw_facility_id"], []), key=lambda d: (d["department_code"], d["department_name"]))
            for department in rows:
                status, category = mapping.get((department["department_code"], department["department_name"]), ("UNMAPPED", ""))
                if status not in {"EXACT", "ALIAS"}:
                    category = ""
                conn.execute("INSERT INTO clinic_mhlw_departments_final VALUES(?,?,?,?,?,?,?,?,?,?)", (
                    int(clinic_id), meta["mhlw_facility_id"], department["department_code"],
                    department["department_name"], category, meta["match_method"], meta["match_confidence"],
                    meta["identity_status"], department["source_date"], status,
                ))
        conn.commit()
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise DeterministicBuildError(f"generated sidecar integrity check failed: {integrity}")
    finally:
        conn.close()
    os.replace(temporary_path, output_path)

    decision_by_clinic = {row["clinic_id"]: row for row in decisions}
    remaining = Counter()
    for row in joins:
        if row["clinic_id"] in final_matches:
            continue
        if row["match_method"] == "UNIQUE_ADDRESS_MATCH":
            remaining[decision_by_clinic[row["clinic_id"]]["final_identity_status"]] += 1
        elif row["join_status"] == "UNMATCHED":
            remaining["UNMATCHED"] += 1
        elif "1:1でない" in row["review_reason"] or "同一住所" in row["review_reason"]:
            remaining["AMBIGUOUS"] += 1
        else:
            remaining["FUZZY_REVIEW"] += 1
    summary = {
        "promotion_decisions": len(decisions), "promotion_allowed": len(allowed),
        "final_matched": len(final_matches), "final_matched_rate": round(len(final_matches) / len(joins), 6),
        "department_summary": aggregate(final_matches, departments, mapping),
        "remaining_total": len(joins) - len(final_matches), "remaining_reason_counts": dict(sorted(remaining.items())),
    }
    if summary_path:
        Path(summary_path).write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=SOURCE_DIR)
    parser.add_argument("--clinic-db", type=Path, default=ROOT / "data" / "clinics.sqlite3")
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--output", type=Path, default=SIDECAR_OUT)
    args = parser.parse_args(argv)
    validate_source_manifest(args.manifest, args.source_dir, args.clinic_db)
    summary = build_sidecar(
        BASE / "phase4_join_v2.csv", BASE / "mhlw_department_master.csv",
        BASE / "mhlw_to_crestix_department_mapping.csv", DECISIONS_PATH,
        args.clinic_db, args.output, SUMMARY_OUT,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
