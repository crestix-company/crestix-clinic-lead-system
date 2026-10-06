"""TASK 8 (phase4 overnight run): join the v2 candidate against the CURRENT
Production clinics.sqlite3 UUID column (read-only, mode=ro) - never cached/
stored as a fixed value on the HP side, re-joined fresh every time per the
task's own rule. Aborts if Production itself has a duplicate UUID (same hard
guard as export_sales_list_to_comdesk.py's verify_uuid_integrity). No write of
any kind to Production.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "sales_target_reclassification"
CLINIC_DB = Path("/Users/maekawahiroyuki/CrestixData/clinic-lead/clinics.sqlite3")


def ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    conn.row_factory = sqlite3.Row
    return conn


def main() -> None:
    with ro(CLINIC_DB) as db:
        dup = db.execute(
            "SELECT uuid, COUNT(*) c FROM clinics WHERE uuid<>'' GROUP BY uuid HAVING c>1"
        ).fetchall()
        if dup:
            raise AssertionError(f"STOP: Production clinics.sqlite3 has {len(dup)} UUID(s) "
                                  f"mapping to >1 clinic: {[dict(d) for d in dup]}")
        total_uuid = db.execute("SELECT COUNT(*) FROM clinics WHERE uuid<>''").fetchone()[0]

        with (OUT / "sales_target_classification_final_v2_candidate.csv").open(
            encoding="utf-8-sig", newline=""
        ) as f:
            v2_rows = list(csv.DictReader(f))
        usable = [r for r in v2_rows if r["sales_tier"] != "UNKNOWN"]
        ids = [int(r["clinic_id"]) for r in usable]

        uuid_by_id = {}
        for chunk_start in range(0, len(ids), 400):
            chunk = ids[chunk_start:chunk_start + 400]
            ph = ",".join("?" * len(chunk))
            for r in db.execute(f"SELECT id, uuid FROM clinics WHERE id IN ({ph})", chunk):
                uuid_by_id[r["id"]] = r["uuid"]

    present = sum(1 for cid in ids if uuid_by_id.get(cid))
    missing = len(ids) - present

    by_tier = {}
    for r in usable:
        tier = r["sales_tier"]
        has = bool(uuid_by_id.get(int(r["clinic_id"])))
        d = by_tier.setdefault(tier, {"uuid_present": 0, "uuid_missing": 0})
        d["uuid_present" if has else "uuid_missing"] += 1

    summary = {
        "production_uuid_integrity": "PASS (0 duplicate UUIDs in clinics.sqlite3)",
        "production_total_uuid_registered": total_uuid,
        "v2_candidate_sales_usable_total": len(usable),
        "v2_candidate_sales_usable_uuid_present": present,
        "v2_candidate_sales_usable_uuid_missing": missing,
        "by_tier": by_tier,
        "join_method": "Fresh read-only join against clinics.sqlite3 at run time (not cached on the "
                        "HP/treatment side), per the rule that Production UUIDs may change in another "
                        "terminal at any time.",
    }
    with (OUT / "uuid_join_summary.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
