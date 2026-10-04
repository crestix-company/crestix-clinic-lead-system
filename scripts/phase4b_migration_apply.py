"""Phase 4-B -> Treatment sidecar Production migration: PHASE B-G.

PHASE B: join page_title/provider_context/exclusion_context/researched_at
         from treatment_results.csv into the 377-row candidate set.
PHASE C: final read-only preflight against the live sidecar, record before-SHA256.
PHASE D: timestamped full backup of the sidecar (Python sqlite3 backup API),
         verified by integrity_check + row count.
PHASE E: BEGIN IMMEDIATE, re-preflight inside the transaction, INSERT 377 rows,
         COMMIT only if every check holds; ROLLBACK and hard-stop otherwise.
PHASE F: read-only post-migration validation.
PHASE G: write production_apply_result.json + production_apply_audit.csv.

Writes ONLY to: the Treatment sidecar (PHASE E, gated), the backups directory
(PHASE D), and artifacts/hybrid_phase4b/migration/ (PHASE G). Never touches
Production clinics.sqlite3, MHLW DB, Comdesk, or the Phase-4B cache. Never re-crawls.
"""
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "hybrid_phase4b"
MIG = OUT / "migration"
SIDECAR = Path("/Users/maekawahiroyuki/CrestixData/clinic-lead/treatment_research_final.sqlite3")
CLINIC_DB = Path("/Users/maekawahiroyuki/CrestixData/clinic-lead/clinics.sqlite3")
MHLW_DB = ROOT / "mhlw_dry_run" / "clinic_mhlw_departments_final.sqlite3"
BACKUP_DIR = Path("/Users/maekawahiroyuki/CrestixData/clinic-lead/backups")

CONFIRMED_EXCLUDED_KEYS = {("5861", "歯列矯正"), ("13029", "歯列矯正")}
REVIEW_EXCLUDED_KEYS = {("303", "採卵")}
ALL_EXCLUDED_KEYS = CONFIRMED_EXCLUDED_KEYS | REVIEW_EXCLUDED_KEYS

MANIFEST_CONFIRMED = "phase4b-fullrun-20261002-auto-confirmed-v1"
MANIFEST_REVIEW = "phase4b-fullrun-20261002-human-verified-review-v1"


class StopMigration(Exception):
    pass


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f) if any(v.strip() for v in r.values())]


def key(row: dict) -> tuple:
    return (row["clinic_id"], row["treatment_category"])


def phase_b_join() -> list[dict]:
    candidates = load_csv(MIG / "production_candidates.csv")
    if len(candidates) != 377:
        raise StopMigration(f"PHASE B: expected 377 candidates, found {len(candidates)}")
    treatments = {key(r): r for r in load_csv(OUT / "treatment_results.csv")}

    joined = []
    join_failures = []
    for c in candidates:
        src = treatments.get(key(c))
        if src is None:
            join_failures.append(key(c))
            continue
        row = dict(c)
        row["page_title"] = src["page_title"]
        row["provider_context"] = src["provider_context"]
        row["exclusion_context"] = src["exclusion_context"]
        row["researched_at"] = src["checked_at"]
        row["_join_status"] = "OK"
        joined.append(row)

    if join_failures:
        raise StopMigration(f"PHASE B: {len(join_failures)} candidate(s) failed to join "
                             f"against treatment_results.csv: {join_failures}")
    if len(joined) != 377:
        raise StopMigration(f"PHASE B: candidate row count changed after join: {len(joined)}")

    missing = {
        "page_title": sum(1 for r in joined if not r["page_title"].strip()),
        "provider_context": sum(1 for r in joined if not r["provider_context"].strip()),
        "exclusion_context": sum(1 for r in joined if not r["exclusion_context"].strip()),
        "researched_at": sum(1 for r in joined if not r["researched_at"].strip()),
    }
    print(f"PHASE B: joined 377/377 rows (0 join failures). "
          f"Blank-but-legitimate field counts (source data, not join failure): {missing}")
    return joined


def phase_c_preflight(joined: list[dict]) -> dict:
    with sqlite3.connect(f"file:{SIDECAR.resolve()}?mode=ro", uri=True) as sdb:
        sdb.execute("PRAGMA query_only=ON")
        sidecar_rows = sdb.execute(
            "SELECT clinic_id, treatment_category_name, research_status FROM clinic_treatment_research_final"
        ).fetchall()
        integrity = sdb.execute("PRAGMA integrity_check").fetchone()[0]
    sidecar_map = {(str(cid), cat): status for cid, cat, status in sidecar_rows}

    candidate_keys = [key(r) for r in joined]
    dup = {k for k in candidate_keys if candidate_keys.count(k) > 1}
    existing_identical = [k for k in candidate_keys if sidecar_map.get(k) == "CONFIRMED"]
    conflict = [k for k in candidate_keys if k in sidecar_map and sidecar_map[k] != "CONFIRMED"]
    invalid = [k for r, k in zip(joined, candidate_keys)
               if r["final_status"] != "CONFIRMED" or not r["clinic_id"].isdigit() or not r["treatment_category"].strip()]
    excluded_present = [k for k in ALL_EXCLUDED_KEYS if k in candidate_keys]
    mentioned_hold = load_csv(MIG / "mentioned_hold.csv")
    mentioned_keys = {key(r) for r in mentioned_hold}
    mentioned_leak = mentioned_keys & set(candidate_keys)

    report = {
        "candidate_count": len(joined),
        "candidate_duplicates": len(dup),
        "sidecar_existing_identical": len(existing_identical),
        "sidecar_conflict": len(conflict),
        "invalid": len(invalid),
        "excluded_rows_present_in_candidates": len(excluded_present),
        "mentioned_rows_present_in_candidates": len(mentioned_leak),
        "sidecar_integrity_check": integrity,
        "sidecar_before_sha256": sha256(SIDECAR),
    }
    bad = {k: v for k, v in report.items()
           if k in {"candidate_duplicates", "sidecar_existing_identical", "sidecar_conflict",
                     "invalid", "excluded_rows_present_in_candidates", "mentioned_rows_present_in_candidates"}
           and v != 0}
    if bad or report["sidecar_integrity_check"] != "ok" or report["candidate_count"] != 377:
        raise StopMigration(f"PHASE C: preflight failed: {report}")
    print(f"PHASE C: preflight OK: {json.dumps(report, ensure_ascii=False)}")
    return report


def phase_d_backup() -> dict:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = BACKUP_DIR / f"treatment_research_final.sqlite3.pre_phase4b_migration_{ts}.sqlite3"

    src_conn = sqlite3.connect(f"file:{SIDECAR.resolve()}?mode=ro", uri=True)
    dst_conn = sqlite3.connect(str(backup_path))
    with dst_conn:
        src_conn.backup(dst_conn)
    src_conn.close()
    dst_conn.close()

    if not backup_path.exists() or backup_path.stat().st_size == 0:
        raise StopMigration(f"PHASE D: backup file missing or empty: {backup_path}")

    # Plain connect (not a shared live DB; nothing writes to this standalone backup file).
    with sqlite3.connect(str(backup_path)) as bdb:
        backup_integrity = bdb.execute("PRAGMA integrity_check").fetchone()[0]
        backup_rowcount = bdb.execute("SELECT COUNT(*) FROM clinic_treatment_research_final").fetchone()[0]
    with sqlite3.connect(f"file:{SIDECAR.resolve()}?mode=ro", uri=True) as sdb:
        sdb.execute("PRAGMA query_only=ON")
        source_rowcount = sdb.execute("SELECT COUNT(*) FROM clinic_treatment_research_final").fetchone()[0]

    if backup_integrity != "ok":
        raise StopMigration(f"PHASE D: backup integrity_check failed: {backup_integrity}")
    if backup_rowcount != source_rowcount:
        raise StopMigration(f"PHASE D: backup rowcount {backup_rowcount} != source rowcount {source_rowcount}")

    backup_sha = sha256(backup_path)
    print(f"PHASE D: backup OK at {backup_path} (sha256={backup_sha}, rows={backup_rowcount}, integrity=ok)")
    return {"backup_path": str(backup_path), "backup_sha256": backup_sha, "backup_rowcount": backup_rowcount}


def phase_e_apply(joined: list[dict], preflight: dict) -> dict:
    git_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()

    conn = sqlite3.connect(str(SIDECAR))
    conn.isolation_level = None  # manual transaction control: BEGIN IMMEDIATE / commit() / rollback()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Re-preflight inside the transaction: the world may have changed since PHASE C.
        cur_rows = conn.execute(
            "SELECT clinic_id, treatment_category_name, research_status FROM clinic_treatment_research_final"
        ).fetchall()
        cur_map = {(str(cid), cat): status for cid, cat, status in cur_rows}
        candidate_keys = [key(r) for r in joined]
        if len(set(candidate_keys)) != 377:
            raise StopMigration("PHASE E: duplicate candidate keys detected inside transaction")
        for k in candidate_keys:
            if k in cur_map:
                raise StopMigration(f"PHASE E: candidate {k} already exists in sidecar "
                                     f"(status={cur_map[k]}) - unexpected since PHASE C; aborting")
        for k in ALL_EXCLUDED_KEYS:
            if k in candidate_keys:
                raise StopMigration(f"PHASE E: excluded key {k} found in candidate set; aborting")

        rows_before = conn.execute("SELECT COUNT(*) FROM clinic_treatment_research_final").fetchone()[0]
        now = datetime.now(timezone.utc).isoformat()
        for r in joined:
            manifest = MANIFEST_REVIEW if r["original_phase4b_status"] == "REVIEW" else MANIFEST_CONFIRMED
            conn.execute(
                """INSERT INTO clinic_treatment_research_final
                   (clinic_id, treatment_category_id, treatment_category_name, research_status,
                    matched_alias, source_url, page_title, provider_context, exclusion_context,
                    evidence_engine_version, taxonomy_version, researched_at, git_commit_sha, manifest_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (int(r["clinic_id"]), r["treatment_category"], r["treatment_category"], "CONFIRMED",
                 r["matched_alias"], r["evidence_url"], r["page_title"], r["provider_context"],
                 r["exclusion_context"], r["evidence_engine_version"], r["taxonomy_version"],
                 r["researched_at"] or now, git_sha, manifest),
            )
        rows_after = conn.execute("SELECT COUNT(*) FROM clinic_treatment_research_final").fetchone()[0]
        inserted = rows_after - rows_before
        if inserted != 377:
            raise StopMigration(f"PHASE E: expected to insert 377 rows, actually inserted {inserted}; aborting")

        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise StopMigration(f"PHASE E: integrity_check failed mid-transaction: {integrity}; aborting")

        conn.commit()
        print(f"PHASE E: COMMIT succeeded. inserted={inserted}, git_commit_sha={git_sha}")
        return {"status": "SUCCESS", "inserted": inserted, "git_commit_sha": git_sha}
    except Exception as exc:
        conn.rollback()
        print(f"PHASE E: ROLLED BACK due to: {exc}", file=sys.stderr)
        return {"status": "ROLLED_BACK", "inserted": 0, "error": str(exc)}
    finally:
        conn.close()


def phase_f_validate(joined: list[dict]) -> dict:
    with sqlite3.connect(f"file:{SIDECAR.resolve()}?mode=ro", uri=True) as sdb:
        sdb.execute("PRAGMA query_only=ON")
        integrity = sdb.execute("PRAGMA integrity_check").fetchone()[0]
        inserted_rows = sdb.execute(
            "SELECT clinic_id, treatment_category_name, manifest_id FROM clinic_treatment_research_final "
            "WHERE manifest_id IN (?, ?)", (MANIFEST_CONFIRMED, MANIFEST_REVIEW),
        ).fetchall()
        dup_check = sdb.execute(
            "SELECT clinic_id, treatment_category_name, COUNT(*) c FROM clinic_treatment_research_final "
            "GROUP BY 1,2 HAVING c > 1"
        ).fetchall()
        excluded_found = []
        for cid, cat in ALL_EXCLUDED_KEYS:
            row = sdb.execute(
                "SELECT 1 FROM clinic_treatment_research_final WHERE clinic_id=? AND treatment_category_name=? "
                "AND manifest_id IN (?, ?)", (int(cid), cat, MANIFEST_CONFIRMED, MANIFEST_REVIEW),
            ).fetchone()
            if row:
                excluded_found.append((cid, cat))
        mentioned_hold = load_csv(MIG / "mentioned_hold.csv")
        mentioned_leak = []
        for r in mentioned_hold:
            row = sdb.execute(
                "SELECT 1 FROM clinic_treatment_research_final WHERE clinic_id=? AND treatment_category_name=? "
                "AND manifest_id IN (?, ?)", (int(r["clinic_id"]), r["treatment_category"], MANIFEST_CONFIRMED, MANIFEST_REVIEW),
            ).fetchone()
            if row:
                mentioned_leak.append(key(r))

    auto_confirmed_present = sum(1 for cid, cat, m in inserted_rows if m == MANIFEST_CONFIRMED)
    human_review_present = sum(1 for cid, cat, m in inserted_rows if m == MANIFEST_REVIEW)

    return {
        "inserted_rows_found": len(inserted_rows),
        "auto_confirmed_present": auto_confirmed_present,
        "human_review_promoted_present": human_review_present,
        "duplicate_clinic_treatment": len(dup_check),
        "excluded_rows_found_in_sidecar": excluded_found,
        "mentioned_rows_found_in_sidecar": mentioned_leak,
        "sidecar_integrity_check": integrity,
    }


def main() -> None:
    clinic_sha_before = sha256(CLINIC_DB)
    mhlw_sha_before = sha256(MHLW_DB)

    joined = phase_b_join()
    preflight = phase_c_preflight(joined)
    backup_info = phase_d_backup()
    apply_result = phase_e_apply(joined, preflight)

    clinic_sha_after = sha256(CLINIC_DB)
    mhlw_sha_after = sha256(MHLW_DB)
    sidecar_after_sha256 = sha256(SIDECAR)

    validation = {}
    if apply_result["status"] == "SUCCESS":
        validation = phase_f_validate(joined)

    confirmed_count = sum(1 for r in joined if r["original_phase4b_status"] == "CONFIRMED")
    review_count = sum(1 for r in joined if r["original_phase4b_status"] == "REVIEW")

    result = {
        "expected_insert": 377,
        "inserted": apply_result["inserted"],
        "auto_confirmed": confirmed_count,
        "human_verified_review_promoted": review_count,
        "excluded": 3,
        "mentioned_held": 180,
        "backup_path": backup_info["backup_path"],
        "backup_sha256": backup_info["backup_sha256"],
        "sidecar_before_sha256": preflight["sidecar_before_sha256"],
        "sidecar_after_sha256": sidecar_after_sha256,
        "integrity_check": validation.get("sidecar_integrity_check", "N/A (rolled back)"),
        "status": apply_result["status"],
        "git_commit_sha": apply_result.get("git_commit_sha"),
        "production_clinics_db_unchanged": clinic_sha_before == clinic_sha_after,
        "mhlw_db_unchanged": mhlw_sha_before == mhlw_sha_after,
        "post_migration_validation": validation,
        "apply_error": apply_result.get("error"),
    }
    (MIG / "production_apply_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    audit_cols = ["clinic_id", "clinic_name", "treatment_category", "matched_alias", "final_status",
                  "original_phase4b_status", "human_verified", "signal_source", "evidence_url",
                  "page_title", "provider_context", "exclusion_context", "researched_at",
                  "evidence_engine_version", "taxonomy_version", "migration_reason", "manifest_id",
                  "apply_status"]
    with (MIG / "production_apply_audit.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=audit_cols)
        w.writeheader()
        for r in joined:
            manifest = MANIFEST_REVIEW if r["original_phase4b_status"] == "REVIEW" else MANIFEST_CONFIRMED
            w.writerow({
                "clinic_id": r["clinic_id"], "clinic_name": r["clinic_name"],
                "treatment_category": r["treatment_category"], "matched_alias": r["matched_alias"],
                "final_status": r["final_status"], "original_phase4b_status": r["original_phase4b_status"],
                "human_verified": r["human_verified"], "signal_source": r["signal_source"],
                "evidence_url": r["evidence_url"], "page_title": r["page_title"],
                "provider_context": r["provider_context"], "exclusion_context": r["exclusion_context"],
                "researched_at": r["researched_at"], "evidence_engine_version": r["evidence_engine_version"],
                "taxonomy_version": r["taxonomy_version"], "migration_reason": r["migration_reason"],
                "manifest_id": manifest, "apply_status": apply_result["status"],
            })

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except StopMigration as exc:
        print(f"MIGRATION STOPPED: {exc}", file=sys.stderr)
        sys.exit(1)
