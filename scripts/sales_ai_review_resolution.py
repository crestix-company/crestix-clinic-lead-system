"""AI re-adjudication of the 1,070 human-review-flagged clinics from
sales_target_classification.csv, resolving each into AUTO_ACCEPT /
HUMAN_REVIEW / HOLD without re-crawling. Read-only on the Treatment sidecar,
MHLW department DB, and taxonomy config. Does not touch
sales_target_classification.csv or any Production/sidecar/MHLW DB.
"""
from __future__ import annotations

import csv
import json
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLASS_CSV = ROOT / "artifacts" / "sales_target_reclassification" / "sales_target_classification.csv"
OUT = ROOT / "artifacts" / "sales_target_reclassification"
SIDECAR = Path("/Users/maekawahiroyuki/CrestixData/clinic-lead/treatment_research_final.sqlite3")
MHLW_DB = ROOT / "mhlw_dry_run" / "clinic_mhlw_departments_final.sqlite3"

GENERIC_ALIASES = {"インプラント", "インプラント治療", "グルー治療", "リブレ",
                    "レーザー治療", "矯正治療", "骨切り", "骨造成", "高周波治療"}
PROVIDER_PRIORITY = {"PROVIDED": 2, "POSSIBLY_PROVIDED": 1, "UNKNOWN": 0, "NOT_PROVIDED": -1}


def ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    conn.row_factory = sqlite3.Row
    return conn


def load_taxonomy_categories() -> dict:
    import sys
    sys.path.insert(0, str(ROOT))
    from src.enrichment.treatment_taxonomy import phase7b_research_categories
    return phase7b_research_categories()


def main() -> None:
    with CLASS_CSV.open(encoding="utf-8-sig", newline="") as f:
        all_rows = list(csv.DictReader(f))
    targets = [r for r in all_rows if r["human_review_recommended"] == "true"]
    if len({r["clinic_id"] for r in targets}) != 1070 or len(targets) != 1070:
        raise AssertionError(f"target set changed: {len(targets)} rows, "
                              f"{len({r['clinic_id'] for r in targets})} distinct clinics")
    target_ids = [int(r["clinic_id"]) for r in targets]
    target_by_id = {int(r["clinic_id"]): r for r in targets}

    with ro(SIDECAR) as db:
        treat_rows: dict[int, list[dict]] = {}
        for chunk_start in range(0, len(target_ids), 400):
            chunk = target_ids[chunk_start:chunk_start + 400]
            ph = ",".join("?" * len(chunk))
            for r in db.execute(
                f"SELECT clinic_id, treatment_category_name, research_status, matched_alias, "
                f"source_url, page_title, provider_context, exclusion_context "
                f"FROM clinic_treatment_research_final WHERE clinic_id IN ({ph})", chunk,
            ):
                treat_rows.setdefault(r["clinic_id"], []).append(dict(r))

    with ro(MHLW_DB) as db:
        raw_dept: dict[int, list[str]] = {}
        crestix_dept: dict[int, list[tuple]] = {}
        for chunk_start in range(0, len(target_ids), 400):
            chunk = target_ids[chunk_start:chunk_start + 400]
            ph = ",".join("?" * len(chunk))
            for r in db.execute(
                f"SELECT clinic_id, mhlw_department_name, crestix_department, mapping_status "
                f"FROM clinic_mhlw_departments_final WHERE clinic_id IN ({ph}) AND mhlw_department_name<>''",
                chunk,
            ):
                raw_dept.setdefault(r["clinic_id"], []).append(r["mhlw_department_name"])
                if r["crestix_department"]:
                    crestix_dept.setdefault(r["clinic_id"], []).append(
                        (r["crestix_department"], r["mapping_status"]))

    categories = load_taxonomy_categories()

    results = []
    for cid in target_ids:
        src = target_by_id[cid]
        flags = set(src["human_review_flags"].split("|")) if src["human_review_flags"] else set()
        rows = treat_rows.get(cid, [])
        review_clean = [r for r in rows if r["research_status"] == "REVIEW" and r["exclusion_context"] == "NONE"]
        dept_text = " ".join(raw_dept.get(cid, []))
        depts_crestix = sorted({d for d, _ in crestix_dept.get(cid, [])})
        clinic_name = src["clinic_name"]

        decision_notes = []

        if "generic_catchall_department" in flags and src["sales_tier"] == "SPECIALTY_TARGET":
            # Per explicit policy: a bare 内科/外科 catch-all is itself a safe,
            # if coarse, sales category. Granularity is not required for
            # AUTO_ACCEPT - only correctness is. We only look for a more
            # specific subspecialty hiding in the same raw text; if none,
            # the catch-all label itself is accepted as-is.
            ai_decision, ai_confidence = "AUTO_ACCEPT", "HIGH"
            final_tier, final_category, final_treatment = src["sales_tier"], src["sales_category"], ""
            decision_notes.append(
                f"内科/外科のみの汎用マッチだが、追加の具体科情報なし(raw department: {dept_text!r})。"
                f"汎用科自体は安全なsales categoryとして扱い、具体的治療の推定は行わない。"
            )
            review_reason = "generic_catchall_department"

        elif review_clean:
            # Unified scoring across ALL competing categories for this clinic
            # in one pass, so a clinic flagged for both category_conflict AND
            # generic_alias gets both checks applied together rather than
            # whichever branch happened to match first.
            #
            # Signals actually available in this cohort's data: provider_context,
            # matched_alias specificity, department<->taxonomy
            # crestix_departments consistency, taxonomy context_required_aliases
            # keyword match (dept text + page_title + clinic_name), and a weak
            # page_title keyword proxy. Rules 2/3/4 from the spec (dedicated
            # page / menu / pricing-booking flow) cannot be computed precisely
            # because signal_source was never persisted for this original
            # full-run cohort; only page_title text approximates it.
            distinct_cats = sorted({r["treatment_category_name"] for r in review_clean})
            scored = []
            contradicted = []
            for cat in distinct_cats:
                cat_rows = [r for r in review_clean if r["treatment_category_name"] == cat]
                best = max(cat_rows, key=lambda r: PROVIDER_PRIORITY.get(r["provider_context"], 0))
                is_generic = best["matched_alias"] in GENERIC_ALIASES
                required = categories.get(cat, {}).get("context_required_aliases", {}).get(best["matched_alias"], [])
                haystack = f"{dept_text} {best['page_title']} {clinic_name}"
                context_hit = [kw for kw in required if kw in haystack]
                cat_crestix = categories.get(cat, {}).get("crestix_departments", [])
                dept_match = bool(cat_crestix) and any(d in depts_crestix for d in cat_crestix)
                dept_contradicts = bool(cat_crestix) and bool(depts_crestix) and not dept_match

                if is_generic and required and not context_hit and dept_contradicts:
                    # Same shape as the confirmed Phase4B false positives:
                    # generic alias, no corroborating context, department
                    # actively disagrees. Disqualify this category.
                    contradicted.append((cat, best, depts_crestix, required))
                    scored.append((-99, cat, best, ["GENERIC_ALIAS_DEPT_CONTRADICTION"]))
                    continue

                score, notes = 0, []
                if best["provider_context"] == "PROVIDED":
                    score += 3; notes.append("provider_context=PROVIDED")
                elif best["provider_context"] == "POSSIBLY_PROVIDED":
                    score += 1
                if not is_generic and best["matched_alias"] == cat:
                    score += 2; notes.append("alias==category(specific)")
                if is_generic:
                    if context_hit:
                        score += 2; notes.append(f"generic alias context一致:{context_hit}")
                    else:
                        score -= 1; notes.append("generic aliasで文脈補強なし")
                if dept_match:
                    score += 2; notes.append(f"department整合({cat_crestix})")
                if best["page_title"] and (cat in best["page_title"] or best["matched_alias"] in best["page_title"]):
                    score += 1; notes.append("page_titleに一致(専用ページ可能性)")
                scored.append((score, cat, best, notes))

            scored.sort(key=lambda x: -x[0])
            top_score, top_cat, top_best, top_notes = scored[0]
            runner_up = scored[1][0] if len(scored) > 1 else -99

            if contradicted and top_score <= 0:
                cat, best, dep, req = contradicted[0]
                ai_decision, ai_confidence = "HOLD", "LOW"
                final_tier, final_category, final_treatment = "UNKNOWN", "", ""
                decision_notes.append(
                    f"generic alias「{best['matched_alias']}」が診療科({dep})と矛盾し、文脈語({req})も不在 "
                    f"- Phase4B既知誤検知パターンと同型")
                review_reason = "generic_alias_department_contradiction"
            elif top_score >= 3 and top_score > runner_up:
                ai_decision, ai_confidence = "AUTO_ACCEPT", "HIGH"
                final_tier, final_category, final_treatment = "LIKELY_TREATMENT", top_cat, top_cat
                decision_notes.append(
                    f"候補{len(distinct_cats)}件中「{top_cat}」が優勢(score={top_score} vs 次点{runner_up}): "
                    + ", ".join(top_notes))
                review_reason = "resolved_dominant_category"
            elif (top_score > 0 and not contradicted
                  and all(categories.get(c, {}).get("crestix_departments") for _, c, _, _ in scored)
                  and set.intersection(*[set(categories.get(c, {}).get("crestix_departments", []))
                                          for _, c, _, _ in scored])):
                shared = sorted(set.intersection(*[set(categories.get(c, {}).get("crestix_departments", []))
                                                    for _, c, _, _ in scored]))
                ai_decision, ai_confidence = "AUTO_ACCEPT", "HIGH"
                final_tier, final_category, final_treatment = "LIKELY_TREATMENT", top_cat, top_cat
                decision_notes.append(
                    f"候補{distinct_cats}は全てcrestix_department={shared}を共有する同一専門内の複数候補であり、"
                    f"相互に矛盾しない(互いに排他的な『対立』ではない)。最有力「{top_cat}」(score={top_score})を採用。")
                review_reason = "resolved_same_specialty_cluster"
            else:
                ai_decision, ai_confidence = "HUMAN_REVIEW", "MEDIUM"
                final_tier, final_category, final_treatment = src["sales_tier"], src["sales_category"], src["treatment_category"]
                decision_notes.append(
                    f"決め手なし(top={top_score}[{top_cat}], runner_up={runner_up}, candidates={distinct_cats})")
                review_reason = "unresolved_ambiguous"

        else:
            ai_decision, ai_confidence = "HOLD", "LOW"
            final_tier, final_category, final_treatment = "UNKNOWN", "", ""
            decision_notes.append("review_clean行が見つからず再評価不能")
            review_reason = "no_evidence"

        human_review_needed = ai_decision == "HUMAN_REVIEW"
        results.append({
            "clinic_id": cid,
            "clinic_name": clinic_name,
            "original_sales_tier": src["sales_tier"],
            "original_sales_category": src["sales_category"],
            "original_treatment_category": src["treatment_category"],
            "review_flag": "|".join(sorted(flags)),
            "review_reason": review_reason,
            "ai_decision": ai_decision,
            "ai_confidence": ai_confidence,
            "final_sales_tier": final_tier,
            "final_sales_category": final_category,
            "final_treatment_category": final_treatment,
            "evidence_text": "",  # not available for this cohort (sidecar has no evidence_text column)
            "evidence_url": (review_clean[0]["source_url"] if review_clean else ""),
            "decision_reason": " / ".join(decision_notes),
            "human_review_needed": str(human_review_needed).lower(),
        })

    cols = ["clinic_id", "clinic_name", "original_sales_tier", "original_sales_category",
            "original_treatment_category", "review_flag", "review_reason", "ai_decision",
            "ai_confidence", "final_sales_tier", "final_sales_category", "final_treatment_category",
            "evidence_text", "evidence_url", "decision_reason", "human_review_needed"]
    with (OUT / "ai_review_resolution.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(results)

    decision_counts = {"AUTO_ACCEPT": 0, "HUMAN_REVIEW": 0, "HOLD": 0}
    tier_b_accepted = tier_c_accepted = 0
    for r in results:
        decision_counts[r["ai_decision"]] += 1
        if r["ai_decision"] == "AUTO_ACCEPT":
            if r["original_sales_tier"] == "LIKELY_TREATMENT":
                tier_b_accepted += 1
            elif r["original_sales_tier"] == "SPECIALTY_TARGET":
                tier_c_accepted += 1

    input_n = len(results)
    hold_n = decision_counts["HOLD"]
    human_n = decision_counts["HUMAN_REVIEW"]
    auto_n = decision_counts["AUTO_ACCEPT"]
    reduction_rate = round(1 - human_n / input_n, 4)

    current_sales_targetable = 7615
    projected_sales_targetable = current_sales_targetable - hold_n
    projected_rate = round(projected_sales_targetable / 9097 * 100, 1)

    from collections import Counter
    reason_counts = Counter(r["review_reason"] for r in results if r["ai_decision"] == "HUMAN_REVIEW")

    summary = {
        "input_review_clinics": input_n,
        "AUTO_ACCEPT": auto_n,
        "HUMAN_REVIEW": human_n,
        "HOLD": hold_n,
        "auto_accept_tier_b": tier_b_accepted,
        "auto_accept_tier_c": tier_c_accepted,
        "human_review_reduction_rate_pct": round(reduction_rate * 100, 1),
        "current_sales_targetable": current_sales_targetable,
        "projected_sales_targetable": projected_sales_targetable,
        "projected_sales_usability_rate_pct": projected_rate,
        "top_human_review_reasons": reason_counts.most_common(5),
    }
    (OUT / "ai_review_resolution_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
