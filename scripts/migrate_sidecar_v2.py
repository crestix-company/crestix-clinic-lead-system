"""One-time live-data migration for the sidecar consistency fix (commit 90bb9fe).

Steps (idempotent; safe to re-run):
  1. Create clinic_research_status if missing (open_final_db already does this).
  2. Backfill clinic_research_status for the 9,399-clinic Full Run from
     progress.sqlite3 (authoritative source for this manifest's attempt state).
  3. Backfill clinic_research_status for the 770 Pilot+Canary clinics (DONE --
     their own result DBs have no FETCH_FAILED-equivalent status at all) from
     the sibling worktree's authoritative result DBs, read-only.
  4. Recreate clinic_treatment_research_final with the new CHECK constraint
     (CONFIRMED/REVIEW/NOT_CONFIRMED only), dropping any remaining
     Treatment-level FETCH_FAILED rows dataset-wide.

Does NOT touch Production DB. Does NOT re-fetch anything -- the 26
stale-row clinics are repaired separately, via the normal research_worker
resume path (see the accompanying report), because an accurate recompute
requires the raw fetched HTML, which the page cache does not retain.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from scripts.research_worker import (
    EVIDENCE_ENGINE_VERSION,
    RULE_VERSION,
    open_final_db,
)

FINAL_DB = Path.home() / "CrestixData/clinic-lead/treatment_research_final.sqlite3"
PROGRESS_DB = Path("data/research_worker/phase7-fullrun-514bd9972b3bf20e/progress.sqlite3")
MANIFEST_ID = "phase7-fullrun-514bd9972b3bf20e"
GIT_COMMIT_SHA = "90bb9fe"  # this migration's own commit; stamped on backfilled rows

OTHER_MHLW_DIR = Path.home() / "Desktop/clinic-list-filter-complete/mhlw_dry_run"
PILOT_DB = OTHER_MHLW_DIR / "phase7b_treatment_research.sqlite3"
CANARY_DB = OTHER_MHLW_DIR / "phase7c_canary_500_v4_canary.sqlite3"


def backfill_fullrun(db: sqlite3.Connection) -> int:
    prog = sqlite3.connect(f"file:{PROGRESS_DB}?mode=ro", uri=True)
    rows = prog.execute(
        "SELECT clinic_id, status, attempts, last_error, candidate_count, finished_at "
        "FROM clinic_progress WHERE status IN ('DONE','FETCH_FAILED')"
    ).fetchall()
    payload = [(
        cid, status, candidate_count, "", "", 0, attempts, last_error or "",
        RULE_VERSION, EVIDENCE_ENGINE_VERSION, MANIFEST_ID, GIT_COMMIT_SHA,
        finished_at or "",
    ) for cid, status, attempts, last_error, candidate_count, finished_at in rows]
    db.executemany(
        "INSERT INTO clinic_research_status "
        "(clinic_id, research_status, candidate_count, source_url, final_url, identity_verified, "
        " attempts, last_error, taxonomy_version, evidence_engine_version, manifest_id, git_commit_sha, researched_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(clinic_id) DO NOTHING",
        payload,
    )
    return len(payload)


def backfill_pilot_canary(db: sqlite3.Connection) -> tuple[int, int]:
    pilot = sqlite3.connect(f"file:{PILOT_DB}?mode=ro", uri=True)
    pilot_ids = {r[0] for r in pilot.execute("SELECT DISTINCT clinic_id FROM clinic_treatment_research")}
    canary = sqlite3.connect(f"file:{CANARY_DB}?mode=ro", uri=True)
    canary_ids = {r[0] for r in canary.execute("SELECT DISTINCT clinic_id FROM canary_treatment_research")}
    overlap = pilot_ids & canary_ids
    if overlap:
        raise ValueError(f"Pilot/Canary clinic_id overlap must be 0, found {len(overlap)}")

    def _insert(ids, source_label):
        payload = [(
            cid, "DONE", 0, "", "", 1, 1, "",
            RULE_VERSION, EVIDENCE_ENGINE_VERSION, source_label, GIT_COMMIT_SHA, "",
        ) for cid in sorted(ids)]
        db.executemany(
            "INSERT INTO clinic_research_status "
            "(clinic_id, research_status, candidate_count, source_url, final_url, identity_verified, "
            " attempts, last_error, taxonomy_version, evidence_engine_version, manifest_id, git_commit_sha, researched_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(clinic_id) DO NOTHING",
            payload,
        )
        return len(payload)

    n_pilot = _insert(pilot_ids, "phase7b-pilot-270")
    n_canary = _insert(canary_ids, "phase7c-canary-500-v4")
    return n_pilot, n_canary


def recreate_treatment_table_with_new_constraint(db: sqlite3.Connection) -> tuple[int, int]:
    before = db.execute("SELECT COUNT(*) FROM clinic_treatment_research_final").fetchone()[0]
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("""
            CREATE TABLE clinic_treatment_research_final_v2 (
                clinic_id INTEGER NOT NULL,
                treatment_category_id TEXT NOT NULL,
                treatment_category_name TEXT NOT NULL,
                research_status TEXT NOT NULL
                    CHECK(research_status IN ('CONFIRMED','REVIEW','NOT_CONFIRMED')),
                matched_alias TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT '',
                page_title TEXT NOT NULL DEFAULT '',
                provider_context TEXT NOT NULL DEFAULT '',
                exclusion_context TEXT NOT NULL DEFAULT '',
                evidence_engine_version TEXT NOT NULL,
                taxonomy_version TEXT NOT NULL,
                researched_at TEXT NOT NULL,
                git_commit_sha TEXT NOT NULL,
                manifest_id TEXT NOT NULL,
                PRIMARY KEY (clinic_id, treatment_category_name)
            )
        """)
        db.execute("""
            INSERT INTO clinic_treatment_research_final_v2
            SELECT clinic_id, treatment_category_id, treatment_category_name, research_status,
                   matched_alias, source_url, page_title, provider_context, exclusion_context,
                   evidence_engine_version, taxonomy_version, researched_at, git_commit_sha, manifest_id
            FROM clinic_treatment_research_final
            WHERE research_status IN ('CONFIRMED','REVIEW','NOT_CONFIRMED')
        """)
        db.execute("DROP TABLE clinic_treatment_research_final")
        db.execute("ALTER TABLE clinic_treatment_research_final_v2 RENAME TO clinic_treatment_research_final")
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    after = db.execute("SELECT COUNT(*) FROM clinic_treatment_research_final").fetchone()[0]
    return before, after


def main() -> int:
    db = open_final_db(FINAL_DB)

    n_fullrun = backfill_fullrun(db)
    print(f"Backfilled clinic_research_status from progress.sqlite3: {n_fullrun} rows (ON CONFLICT DO NOTHING)")

    n_pilot, n_canary = backfill_pilot_canary(db)
    print(f"Backfilled clinic_research_status for Pilot: {n_pilot}, Canary: {n_canary}")

    counts = dict(db.execute("SELECT research_status, COUNT(*) FROM clinic_research_status GROUP BY research_status"))
    print(f"clinic_research_status totals: {counts}")

    before, after = recreate_treatment_table_with_new_constraint(db)
    print(f"clinic_treatment_research_final rows: {before} -> {after} (dropped {before - after} FETCH_FAILED rows)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
