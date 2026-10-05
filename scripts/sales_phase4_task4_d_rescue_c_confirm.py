"""TASK 4 (phase4 overnight run): re-verify all 22 RESCUE_C candidates by
directly querying the MHLW department sidecar (read-only) for an EXACT/ALIAS
crestix_department mapping matching the rescue's claimed department - rather
than trusting the prior session's reason text. No treatment-level claim is
made for any of these (sales_category values are the generic department-level
buckets, i.e. Tier C / SPECIALTY_TARGET only). No re-crawl, no sidecar write.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "sales_target_reclassification" / "d_unknown_rescue"
MHLW_DB = ROOT / "mhlw_dry_run" / "clinic_mhlw_departments_final.sqlite3"


def ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    conn.row_factory = sqlite3.Row
    return conn


def main() -> None:
    with (OUT / "d_rescue_candidates.csv").open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if len(rows) != 22:
        raise AssertionError(f"expected 22 RESCUE_C candidates, got {len(rows)}")

    with ro(MHLW_DB) as db:
        results = []
        for r in rows:
            cid = int(r["clinic_id"])
            mhlw_rows = [dict(x) for x in db.execute(
                "SELECT mhlw_department_name, crestix_department, mapping_status "
                "FROM clinic_mhlw_departments_final WHERE clinic_id=?", (cid,))]
            matched = [m for m in mhlw_rows if m["crestix_department"] == r["crestix_department"]
                       and m["mapping_status"] in ("EXACT", "ALIAS")]
            conflicting_exclude = [m for m in mhlw_rows if m["mhlw_department_name"]
                                    and m["mapping_status"] == "EXCLUDE"
                                    and m["crestix_department"] == r["crestix_department"]]
            if matched:
                decision = "CONFIRM_RESCUE_C"
                reason = (f"MHLW sidecar再照会で確認: {matched[0]['mhlw_department_name']} -> "
                          f"{matched[0]['crestix_department']} ({matched[0]['mapping_status']})")
            else:
                decision = "REJECT_RESCUE"
                reason = (f"MHLW sidecar再照会でEXACT/ALIASマッピングが見つからない。"
                          f"全行: {mhlw_rows}")
            if conflicting_exclude:
                reason += f" | 注: 同じcrestix_department値に対するEXCLUDE行も存在: {conflicting_exclude}"

            row = dict(r)
            row["mhlw_recheck_decision"] = decision
            row["mhlw_recheck_reason"] = reason
            results.append(row)

    out_path = OUT / "d_rescue_final_review.csv"
    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        w.writeheader()
        w.writerows(results)

    from collections import Counter
    counts = Counter(r["mhlw_recheck_decision"] for r in results)
    summary = {
        "scope": "22 RESCUE_C candidates (d_rescue_candidates.csv)",
        "counts": dict(counts),
        "method": "Direct re-query of clinic_mhlw_departments_final.sqlite3 (read-only) for each "
                   "clinic_id, independent of the prior session's reason text.",
    }
    with (OUT / "d_rescue_final_review_summary.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
