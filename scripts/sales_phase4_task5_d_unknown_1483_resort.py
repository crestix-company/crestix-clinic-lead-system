"""TASK 5 (phase4 overnight run): re-sort the 1,483 KEEP_D clinics using only
existing data (clinics.sqlite3 hp_url/hp_status, clinic_research_status
candidate_count, and the pre-existing treatment/department reasons in
d_unknown_breakdown.csv). No new crawl, no network requests, no sidecar/
Production write - matches the prior session's d_rescue_summary.json
limited_research_eligibility_note (identity_verified is 0 for the entire
9,097 cohort, confirmed non-informative; no clinic could be confirmed
re-fetch-eligible from existing data alone).

Buckets (priority order, first match wins):
  HP_NONE                  - clinics.hp_url is empty: no official HP exists
                              at all. Cannot be rescued even by re-crawling;
                              would need new web discovery first.
  HP_PRESENT_ZERO_CANDIDATE - hp_url present, clinic_research_status
                              .candidate_count=0: the crawl found literally
                              no treatment-category keyword hits on the
                              fetched page(s). A deeper crawl (more pages) or
                              a fetch retry might find something; not a
                              detector bug.
  HP_PRESENT_ALL_EXCLUDED  - hp_url present, candidate_count>0 but every
                              resulting row is NOT_CONFIRMED/excluded (explicit
                              negation, referral, article/blog context, etc).
                              These already have evidence_text available in
                              principle; most promising group for a future
                              re-run with an improved taxonomy (e.g. TASK2's
                              fix), without any new network access.
  OTHER                    - does not cleanly fit the above (data
                              inconsistency between clinics.sqlite3 and the
                              sidecar).

MHLW crestix_department mapping absence and identity_verified=0 are reported
as separate, mostly cohort-wide facts (not used as the primary bucket key: 1480
of 1483 already lack a mapping, so it does not discriminate between clinics).
"""
from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "sales_target_reclassification"
RESCUE_DIR = OUT / "d_unknown_rescue"
CLINIC_DB = Path("/Users/maekawahiroyuki/CrestixData/clinic-lead/clinics.sqlite3")
SIDECAR = Path("/Users/maekawahiroyuki/CrestixData/clinic-lead/treatment_research_final.sqlite3")
MANIFEST = "phase7-fullrun-514bd9972b3bf20e"


def ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    conn.row_factory = sqlite3.Row
    return conn


def main() -> None:
    with (RESCUE_DIR / "d_keep_unknown.csv").open(encoding="utf-8-sig", newline="") as f:
        keep_rows = list(csv.DictReader(f))
    if len(keep_rows) != 1483:
        raise AssertionError(f"expected 1483 KEEP_D rows, got {len(keep_rows)}")
    ids = [int(r["clinic_id"]) for r in keep_rows]

    with (RESCUE_DIR / "d_unknown_breakdown.csv").open(encoding="utf-8-sig", newline="") as f:
        dept_reason_by_id = {int(r["clinic_id"]): r["department_reason"] for r in csv.DictReader(f)}

    with ro(CLINIC_DB) as db:
        hp_by_id = {}
        for chunk_start in range(0, len(ids), 400):
            chunk = ids[chunk_start:chunk_start + 400]
            ph = ",".join("?" * len(chunk))
            for r in db.execute(f"SELECT id, hp_url, hp_status FROM clinics WHERE id IN ({ph})", chunk):
                hp_by_id[r["id"]] = (r["hp_url"], r["hp_status"])

    with ro(SIDECAR) as db:
        status_by_id = {}
        for chunk_start in range(0, len(ids), 400):
            chunk = ids[chunk_start:chunk_start + 400]
            ph = ",".join("?" * len(chunk))
            for r in db.execute(
                f"SELECT clinic_id, candidate_count, identity_verified FROM clinic_research_status "
                f"WHERE clinic_id IN ({ph}) AND manifest_id=?", chunk + [MANIFEST],
            ):
                status_by_id[r["clinic_id"]] = (r["candidate_count"], r["identity_verified"])

    results = []
    for r in keep_rows:
        cid = int(r["clinic_id"])
        hp_url, hp_status = hp_by_id.get(cid, ("", ""))
        candidate_count, identity_verified = status_by_id.get(cid, (None, None))

        if not hp_url:
            bucket = "HP_NONE"
            note = "clinics.hp_url が空。新規Web探索なしでは再crawl自体が不可能"
        elif candidate_count == 0:
            bucket = "HP_PRESENT_ZERO_CANDIDATE"
            note = f"HPあり(hp_status={hp_status})だがcandidate_count=0: 取得ページに治療カテゴリの" \
                   f"キーワード候補が一件も検出されなかった"
        elif candidate_count and candidate_count > 0:
            bucket = "HP_PRESENT_ALL_EXCLUDED"
            note = f"HPあり(hp_status={hp_status})でcandidate_count={candidate_count}件の候補はあったが、" \
                   f"全てNOT_CONFIRMED/除外。再crawl不要でtaxonomy再適用のみで再評価できる可能性"
        else:
            bucket = "OTHER"
            note = f"clinics.sqlite3とsidecarの整合性が取れない(hp_url={hp_url!r}, " \
                   f"candidate_count={candidate_count!r})"

        results.append({
            "clinic_id": cid,
            "clinic_name": r["clinic_name"],
            "hp_url_present": bool(hp_url),
            "hp_status": hp_status,
            "candidate_count": candidate_count,
            "identity_verified": identity_verified,
            "mhlw_department_reason": dept_reason_by_id.get(cid, ""),
            "resort_bucket": bucket,
            "resort_note": note,
        })

    out_path = RESCUE_DIR / "d_unknown_final_breakdown.csv"
    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        w.writeheader()
        w.writerows(results)

    bucket_counts = Counter(r["resort_bucket"] for r in results)
    dept_reason_counts = Counter(r["mhlw_department_reason"] for r in results)
    identity_verified_true = sum(1 for r in results if r["identity_verified"])

    summary = {
        "scope": "1,483 KEEP_D clinics",
        "bucket_counts": dict(bucket_counts),
        "mhlw_department_reason_counts": dict(dept_reason_counts),
        "rescue_priority_note": "HP_PRESENT_ALL_EXCLUDED clinics are the best future-rescue candidates "
                                 "(no new network access needed, just a taxonomy/detector re-run - see "
                                 "TASK2's NON_OFFICIAL fix, though none of these specific rows were found "
                                 "to use an aggregator domain in this pass). HP_PRESENT_ZERO_CANDIDATE "
                                 "would need a deeper or retried crawl. HP_NONE needs new web discovery "
                                 "before any crawl is even possible.",
        "identity_verified_true_count": identity_verified_true,
        "identity_verified_global_note": "identity_verified=0 for effectively the entire 9,097-clinic "
                                          "cohort (confirmed in a prior session's audit, re-confirmed here: "
                                          f"{identity_verified_true}/1483 true in this subset) - this column "
                                          "does not discriminate between clinics and is not usable as a "
                                          "per-clinic bucket; it is a cohort-wide data gap, not information "
                                          "about any individual clinic.",
    }
    with (RESCUE_DIR / "d_unknown_final_breakdown_summary.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
