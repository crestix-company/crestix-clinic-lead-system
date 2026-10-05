"""TASK 6+7 (phase4 overnight run): build sales_target_classification_final_v2_candidate.csv
from sales_target_classification_final.csv (NEVER overwritten/modified in place - this script
only reads it) plus the confirmed deltas from TASK1/TASK3/TASK4. Read-only on Treatment
sidecar/MHLW DB. UNCERTAIN/KEEP_REVIEW/HOLD rows are left unchanged, per the no-forced-
resolution safety rule.

Deltas applied:
  A. 22 confirmed RESCUE_C clinics (TASK4 d_rescue_final_review.csv, all CONFIRM_RESCUE_C):
     UNKNOWN -> SPECIALTY_TARGET at the stated crestix_department, sales_usable=true.
  B. 11 of the 73 combined FALSE_POSITIVE rows (TASK1) whose category is the clinic's
     CURRENT driving treatment_category in the final CSV (i.e. removing it actually changes
     something). For each: drop the false claim, then look at the clinic's *other* sidecar
     rows with research_status=REVIEW and exclusion_context=NONE, keep only those whose
     treatment_categories[*].crestix_departments overlaps the clinic's actual MHLW crestix_
     department (EXACT/ALIAS). Exactly one survivor -> reassign to it at LIKELY_TREATMENT
     (this reuses the exact same department tie-break rule as TASK3, and for clinic_id 662/
     4666 independently reaches the identical target category TASK3 already found - two
     independent methods agreeing). Zero or multiple survivors -> fall back to
     SPECIALTY_TARGET via the clinic's MHLW EXACT/ALIAS mapping if any, else UNKNOWN.
     The other 62 FALSE_POSITIVE rows concern a category that was never the clinic's driving
     assignment in the final CSV, so there is nothing to change for them.
  C. 11 confirmed AUTO_RESOLVE clinics (TASK3 human_review_priority_resolution.csv): tier/
     category is already correct, this only clears human_review_needed. The 8
     AUTO_RESOLVE_RECLASSIFY clinics are NOT auto-applied here (left as a recommendation in
     the TASK3 artifact) except for 662/4666, which are applied via path B above because two
     independent analyses agree on the same target category.
"""
from __future__ import annotations

import csv
import json
import sqlite3
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "sales_target_reclassification"
SIDECAR = Path("/Users/maekawahiroyuki/CrestixData/clinic-lead/treatment_research_final.sqlite3")
MHLW_DB = ROOT / "mhlw_dry_run" / "clinic_mhlw_departments_final.sqlite3"
TAXONOMY_YML = ROOT / "config" / "treatment_taxonomy.yml"

sys.path.insert(0, str(ROOT))

GENERIC_SALES_CATEGORY = {
    "皮膚科": "皮膚科一般", "消化器内科": "消化器内科一般", "循環器内科": "循環器内科一般",
    "眼科": "眼科一般", "糖尿病内科": "糖尿病内科一般", "泌尿器科": "泌尿器科一般",
    "産婦人科": "婦人科/産科", "美容整形外科": "美容整形外科一般", "歯科": "一般歯科",
}


def ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    conn.row_factory = sqlite3.Row
    return conn


def main() -> None:
    taxonomy = yaml.safe_load(TAXONOMY_YML.read_text(encoding="utf-8"))
    cats = taxonomy["treatment_categories"]

    def expected_departments(category: str) -> set[str]:
        return set(cats.get(category, {}).get("crestix_departments", ()))

    with (OUT / "sales_target_classification_final.csv").open(encoding="utf-8-sig", newline="") as f:
        final_rows = list(csv.DictReader(f))
    if len(final_rows) != 9097:
        raise AssertionError(f"expected 9097 base rows, got {len(final_rows)}")
    by_id = {int(r["clinic_id"]): r for r in final_rows}
    v2 = {cid: dict(r) for cid, r in by_id.items()}

    change_log = []

    # ---- A. RESCUE_C (TASK4) ----
    with (OUT / "d_unknown_rescue" / "d_rescue_final_review.csv").open(encoding="utf-8-sig", newline="") as f:
        rescue_rows = list(csv.DictReader(f))
    confirmed_rescue = [r for r in rescue_rows if r["mhlw_recheck_decision"] == "CONFIRM_RESCUE_C"]
    if len(confirmed_rescue) != 22:
        raise AssertionError(f"expected 22 confirmed RESCUE_C, got {len(confirmed_rescue)}")
    for r in confirmed_rescue:
        cid = int(r["clinic_id"])
        before = dict(v2[cid])
        v2[cid].update({
            "sales_tier": "SPECIALTY_TARGET",
            "sales_category": r["sales_category"],
            "treatment_category": "",
            "confidence": r["confidence"],
            "sales_usable": "true",
            "classification_source": "phase4_task4_rescue_c_confirmed",
            "human_verified": "false",
            "human_review_needed": "false",
            "crestix_department": r["crestix_department"],
            "decision_reason": r["mhlw_recheck_reason"],
        })
        change_log.append({"clinic_id": cid, "change": "RESCUE_C", "before_tier": before["sales_tier"],
                            "after_tier": "SPECIALTY_TARGET", "after_category": r["sales_category"]})

    # ---- B. FALSE_POSITIVE corrections that match the clinic's current driving category ----
    fp_pairs = []
    with (OUT / "generic_alias_mismatch_review_final.csv").open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if r["reverified_ai_decision"] == "FALSE_POSITIVE":
                fp_pairs.append((int(r["clinic_id"]), r["treatment_category"]))
    with (OUT / "broad_alias_and_generic_alias_origin_review.csv").open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if r["ai_decision"] == "FALSE_POSITIVE":
                fp_pairs.append((int(r["clinic_id"]), r["treatment_category"]))
    if len(fp_pairs) != 73:
        raise AssertionError(f"expected 73 combined FALSE_POSITIVE pairs, got {len(fp_pairs)}")

    affected = [(cid, cat) for cid, cat in fp_pairs
                if cid in by_id and by_id[cid]["treatment_category"] == cat]

    with ro(SIDECAR) as sdb, ro(MHLW_DB) as mdb:
        for cid, fp_cat in affected:
            other_review = [dict(x) for x in sdb.execute(
                "SELECT treatment_category_name FROM clinic_treatment_research_final "
                "WHERE clinic_id=? AND treatment_category_name<>? AND research_status='REVIEW' "
                "AND exclusion_context='NONE'", (cid, fp_cat))]
            actual_depts = {d["crestix_department"] for d in mdb.execute(
                "SELECT DISTINCT crestix_department FROM clinic_mhlw_departments_final "
                "WHERE clinic_id=? AND mapping_status IN ('EXACT','ALIAS') AND crestix_department<>''",
                (cid,))}
            survivors = [r["treatment_category_name"] for r in other_review
                         if actual_depts & expected_departments(r["treatment_category_name"])]
            survivors = sorted(set(survivors))

            before = dict(v2[cid])
            if len(survivors) == 1:
                new_cat = survivors[0]
                v2[cid].update({
                    "sales_tier": "LIKELY_TREATMENT", "sales_category": new_cat,
                    "treatment_category": new_cat, "confidence": "MEDIUM",
                    "classification_source": "phase4_task1_fp_correction_reassigned",
                    "human_review_needed": "false",
                    "decision_reason": f"TASK1でFALSE_POSITIVE確定({fp_cat})。他のREVIEW/NONE候補中、"
                                        f"診療科({actual_depts})が一致するのは「{new_cat}」のみのため再割当",
                })
                outcome = f"REASSIGNED to {new_cat}"
            else:
                dept_exact = [d["crestix_department"] for d in mdb.execute(
                    "SELECT DISTINCT crestix_department FROM clinic_mhlw_departments_final "
                    "WHERE clinic_id=? AND mapping_status='EXACT' AND crestix_department<>''", (cid,))]
                if dept_exact:
                    dept = dept_exact[0]
                    v2[cid].update({
                        "sales_tier": "SPECIALTY_TARGET",
                        "sales_category": GENERIC_SALES_CATEGORY.get(dept, dept + "一般"),
                        "treatment_category": "", "confidence": "MEDIUM",
                        "classification_source": "phase4_task1_fp_correction_downgraded_to_C",
                        "human_review_needed": "true" if len(survivors) > 1 else "false",
                        "decision_reason": f"TASK1でFALSE_POSITIVE確定({fp_cat})。他候補{survivors or 'なし'}は"
                                            f"診療科と{'複数一致' if len(survivors) > 1 else '不一致'}のため、"
                                            f"MHLW EXACT部門({dept})のSPECIALTY_TARGETへ降格",
                    })
                    outcome = f"DOWNGRADED to C ({dept})"
                else:
                    v2[cid].update({
                        "sales_tier": "UNKNOWN", "sales_category": "", "treatment_category": "",
                        "confidence": "", "sales_usable": "false",
                        "classification_source": "phase4_task1_fp_correction_downgraded_to_D",
                        "human_review_needed": "false",
                        "decision_reason": f"TASK1でFALSE_POSITIVE確定({fp_cat})。他候補も部門マッピングも"
                                            f"根拠なく、UNKNOWNへ降格",
                    })
                    outcome = "DOWNGRADED to D"
            change_log.append({"clinic_id": cid, "change": "FP_CORRECTION", "fp_category": fp_cat,
                                "before_tier": before["sales_tier"], "outcome": outcome})

    # ---- C. AUTO_RESOLVE (TASK3) - clears human_review_needed only ----
    with (OUT / "human_review_priority_resolution.csv").open(encoding="utf-8-sig", newline="") as f:
        resolution_rows = list(csv.DictReader(f))
    auto_resolve = [r for r in resolution_rows if r["resolution_decision"] == "AUTO_RESOLVE"]
    if len(auto_resolve) != 11:
        raise AssertionError(f"expected 11 AUTO_RESOLVE rows, got {len(auto_resolve)}")
    for r in auto_resolve:
        cid = int(r["clinic_id"])
        if cid not in v2:
            continue
        v2[cid]["human_review_needed"] = "false"
        v2[cid]["classification_source"] = v2[cid].get("classification_source", "") + "+phase4_task3_auto_resolve"
        change_log.append({"clinic_id": cid, "change": "AUTO_RESOLVE_CONFIRMED",
                            "tier": v2[cid]["sales_tier"], "category": v2[cid]["sales_category"]})

    out_rows = [v2[cid] for cid in sorted(v2)]
    out_path = OUT / "sales_target_classification_final_v2_candidate.csv"
    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(final_rows[0].keys()))
        w.writeheader()
        w.writerows(out_rows)

    with (OUT / "v2_candidate_change_log.json").open("w", encoding="utf-8") as f:
        json.dump(change_log, f, ensure_ascii=False, indent=2)

    # ---- TASK7 projection ----
    from collections import Counter
    current = Counter(r["sales_tier"] for r in final_rows)
    projected = Counter(r["sales_tier"] for r in out_rows)
    tier_map = {"VERIFIED_TREATMENT": "A", "LIKELY_TREATMENT": "B", "SPECIALTY_TARGET": "C", "UNKNOWN": "D"}
    current_usable = sum(1 for r in final_rows if r["sales_tier"] != "UNKNOWN")
    projected_usable = sum(1 for r in out_rows if r["sales_tier"] != "UNKNOWN")

    n_reassigned = sum(1 for c in change_log if c.get("change") == "FP_CORRECTION" and "REASSIGNED" in c.get("outcome", ""))
    n_downgraded_c = sum(1 for c in change_log if c.get("change") == "FP_CORRECTION" and "DOWNGRADED to C" in c.get("outcome", ""))
    n_downgraded_d = sum(1 for c in change_log if c.get("change") == "FP_CORRECTION" and "DOWNGRADED to D" in c.get("outcome", ""))

    projection = {
        "current": {tier_map[k]: v for k, v in current.items()} | {"usable": current_usable},
        "candidate_v2": {tier_map[k]: v for k, v in projected.items()} | {"usable": projected_usable},
        "changes": {
            "rescue_c_added": 22,
            "fp_correction_reassigned_to_B": n_reassigned,
            "fp_correction_downgraded_to_C": n_downgraded_c,
            "fp_correction_downgraded_to_D": n_downgraded_d,
            "fp_correction_total_rows_touched": len(affected),
            "fp_correction_rows_with_no_effect_other_category": 73 - len(affected),
            "human_review_auto_resolve_confirmed": 11,
        },
    }
    with (OUT / "final_v2_candidate_summary.json").open("w", encoding="utf-8") as f:
        json.dump(projection, f, ensure_ascii=False, indent=2)
    print(json.dumps(projection, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
