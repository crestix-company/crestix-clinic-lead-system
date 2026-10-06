"""Phase 2 of the A/B/C sales-list finalization (PHASES 1-7 of the request):

1. Stratified 100-clinic human-audit sample for the 255
   resolved_same_specialty_cluster AUTO_ACCEPT decisions.
2. same_specialty_cluster_human_audit_100.csv
3. Mechanical risk audit over all 255 cluster decisions ->
   SAFE_CLUSTER / REVIEW_CLUSTER / REJECT_CLUSTER.
4-6. Second-pass AI resolution of the 287 remaining HUMAN_REVIEW clinics,
   aiming to minimize (not force down) the remaining count.
7. Provisional final A/B/C preview CSV (does not overwrite any existing
   artifact).

Read-only on the Treatment sidecar, MHLW DB, clinic master, and taxonomy
config. Never re-crawls. Writes only new files under
artifacts/sales_target_reclassification/.
"""
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "sales_target_reclassification"
SIDECAR = Path("/Users/maekawahiroyuki/CrestixData/clinic-lead/treatment_research_final.sqlite3")
CLINIC_DB = Path("/Users/maekawahiroyuki/CrestixData/clinic-lead/clinics.sqlite3")
MHLW_DB = ROOT / "mhlw_dry_run" / "clinic_mhlw_departments_final.sqlite3"
SEED = "sales-phase2-audit-20261005-v1"

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


def stable_hash(*parts: str) -> str:
    return hashlib.sha256(f"{SEED}|{'|'.join(parts)}".encode()).hexdigest()


def main() -> None:
    with (OUT / "ai_review_resolution.csv").open(encoding="utf-8-sig", newline="") as f:
        resolution_rows = list(csv.DictReader(f))
    if len(resolution_rows) != 1070 or len({r["clinic_id"] for r in resolution_rows}) != 1070:
        raise AssertionError("ai_review_resolution.csv changed since last task")

    cluster_rows = [r for r in resolution_rows if r["review_reason"] == "resolved_same_specialty_cluster"]
    cluster_ids = sorted({int(r["clinic_id"]) for r in cluster_rows})
    if len(cluster_ids) != 255:
        raise AssertionError(f"PHASE 1: expected 255 resolved_same_specialty_cluster clinics, got {len(cluster_ids)}")
    print(f"PHASE 1: confirmed 255 distinct resolved_same_specialty_cluster clinics.")

    human_review_rows = [r for r in resolution_rows if r["ai_decision"] == "HUMAN_REVIEW"]
    human_review_ids = sorted({int(r["clinic_id"]) for r in human_review_rows})
    if len(human_review_ids) != 287:
        raise AssertionError(f"PHASE 4: expected 287 HUMAN_REVIEW clinics, got {len(human_review_ids)}")

    all_target_ids = sorted(set(cluster_ids) | set(human_review_ids) |
                             {int(r["clinic_id"]) for r in resolution_rows})

    # ---- gather full source data for everyone touched this round ----
    with ro(SIDECAR) as db:
        cohort_ids = [r[0] for r in db.execute(
            "SELECT clinic_id FROM clinic_research_status WHERE manifest_id=? AND research_status='DONE'",
            ("phase7-fullrun-514bd9972b3bf20e",))]
        if len(cohort_ids) != 9097:
            raise AssertionError("9,097 cohort reconstruction changed")

        treat_rows: dict[int, list[dict]] = {}
        for chunk_start in range(0, len(all_target_ids), 400):
            chunk = all_target_ids[chunk_start:chunk_start + 400]
            ph = ",".join("?" * len(chunk))
            for r in db.execute(
                f"SELECT clinic_id, treatment_category_name, research_status, matched_alias, "
                f"source_url, page_title, provider_context, exclusion_context "
                f"FROM clinic_treatment_research_final WHERE clinic_id IN ({ph})", chunk,
            ):
                treat_rows.setdefault(r["clinic_id"], []).append(dict(r))

    with ro(CLINIC_DB) as db:
        clinic_meta: dict[int, dict] = {}
        for chunk_start in range(0, len(all_target_ids), 400):
            chunk = all_target_ids[chunk_start:chunk_start + 400]
            ph = ",".join("?" * len(chunk))
            for r in db.execute(
                f"SELECT id, clinic_name, prefecture, medical_type FROM clinics WHERE id IN ({ph})", chunk,
            ):
                clinic_meta[r["id"]] = dict(r)

    with ro(MHLW_DB) as db:
        raw_dept: dict[int, list[str]] = {}
        crestix_dept: dict[int, list[tuple]] = {}
        for chunk_start in range(0, len(all_target_ids), 400):
            chunk = all_target_ids[chunk_start:chunk_start + 400]
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

    # ================= PHASE 3: risk audit over all 255 =================
    risk_rows = []
    risk_counts = Counter()
    for cid in cluster_ids:
        res = next(r for r in cluster_rows if int(r["clinic_id"]) == cid)
        rows = treat_rows.get(cid, [])
        review_clean = [r for r in rows if r["research_status"] == "REVIEW" and r["exclusion_context"] == "NONE"]
        all_review = [r for r in rows if r["research_status"] == "REVIEW"]
        depts_crestix = sorted({d for d, _ in crestix_dept.get(cid, [])})

        issues = []
        distinct_cats = sorted({r["treatment_category_name"] for r in review_clean})
        cat_depts = [frozenset(categories.get(c, {}).get("crestix_departments", [])) for c in distinct_cats]
        shared = set.intersection(*[set(x) for x in cat_depts]) if cat_depts else set()
        if not shared:
            issues.append("NO_SHARED_DEPARTMENT")
        if not depts_crestix:
            issues.append("DEPARTMENT_MAPPING_MISSING_FOR_CLINIC")
        generic_hit = [r["matched_alias"] for r in review_clean if r["matched_alias"] in GENERIC_ALIASES]
        if generic_hit:
            issues.append(f"GENERIC_ALIAS_PRESENT:{sorted(set(generic_hit))}")
        if any(r["exclusion_context"] != "NONE" for r in all_review if r["treatment_category_name"] in distinct_cats):
            issues.append("EXCLUSION_CONTEXT_NOT_NONE_ON_SOME_ROW")
        weak_provider = [c for c in distinct_cats
                          if max((PROVIDER_PRIORITY.get(r["provider_context"], 0) for r in review_clean
                                  if r["treatment_category_name"] == c), default=0) < 1]
        if weak_provider:
            issues.append(f"WEAK_PROVIDER_CONTEXT:{weak_provider}")
        missing_url = [r["treatment_category_name"] for r in review_clean if not r["source_url"].strip()]
        if missing_url:
            issues.append("EVIDENCE_URL_MISSING")
        # evidence_text is categorically unavailable for this cohort's schema
        issues.append("EVIDENCE_TEXT_UNAVAILABLE_SCHEMA_GAP")
        # mutually-exclusive category check: categories that explicitly list
        # DIFFERENT crestix_departments with zero overlap pairwise
        exclusive_pairs = []
        for i in range(len(distinct_cats)):
            for j in range(i + 1, len(distinct_cats)):
                di, dj = cat_depts[i], cat_depts[j]
                if di and dj and not (di & dj):
                    exclusive_pairs.append((distinct_cats[i], distinct_cats[j]))
        if exclusive_pairs:
            issues.append(f"MUTUALLY_EXCLUSIVE_CATEGORY_PAIR:{exclusive_pairs}")

        hard_issues = [i for i in issues if i not in ("EVIDENCE_TEXT_UNAVAILABLE_SCHEMA_GAP",)
                       and not i.startswith("EVIDENCE_URL_MISSING")]
        if "NO_SHARED_DEPARTMENT" in issues or any(i.startswith("MUTUALLY_EXCLUSIVE") for i in issues):
            verdict = "REJECT_CLUSTER"
        elif "DEPARTMENT_MAPPING_MISSING_FOR_CLINIC" in issues or any(i.startswith("GENERIC_ALIAS") for i in issues) \
                or any(i.startswith("WEAK_PROVIDER") for i in issues) or "EXCLUSION_CONTEXT_NOT_NONE_ON_SOME_ROW" in issues \
                or "EVIDENCE_URL_MISSING" in issues:
            verdict = "REVIEW_CLUSTER"
        else:
            verdict = "SAFE_CLUSTER"
        risk_counts[verdict] += 1
        risk_rows.append({
            "clinic_id": cid, "clinic_name": clinic_meta.get(cid, {}).get("clinic_name", ""),
            "candidate_categories": " / ".join(distinct_cats),
            "shared_crestix_department": " / ".join(sorted(shared)) if shared else "",
            "clinic_crestix_department": " / ".join(depts_crestix),
            "final_treatment_category": res["final_sales_category"],
            "issues": " | ".join(issues),
            "risk_verdict": verdict,
        })

    with (OUT / "same_specialty_cluster_risk_audit.csv").open("w", encoding="utf-8-sig", newline="") as f:
        cols = ["clinic_id", "clinic_name", "candidate_categories", "shared_crestix_department",
                "clinic_crestix_department", "final_treatment_category", "issues", "risk_verdict"]
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(risk_rows)
    print(f"PHASE 3: risk audit written. {dict(risk_counts)}")

    # ======= PHASE 1b/2: stratified 100-sample human audit CSV =======
    def strata_key(cid: int) -> tuple:
        res = next(r for r in cluster_rows if int(r["clinic_id"]) == cid)
        rows = treat_rows.get(cid, [])
        review_clean = [r for r in rows if r["research_status"] == "REVIEW" and r["exclusion_context"] == "NONE"]
        best = max(review_clean, key=lambda r: PROVIDER_PRIORITY.get(r["provider_context"], 0)) if review_clean else None
        meta = clinic_meta.get(cid, {})
        depts_crestix = sorted({d for d, _ in crestix_dept.get(cid, [])})
        return (
            res["final_sales_category"],
            depts_crestix[0] if depts_crestix else "(none)",
            len({r["treatment_category_name"] for r in review_clean}),
            best["provider_context"] if best else "(none)",
            best["matched_alias"] if best else "(none)",
            meta.get("prefecture", "(none)"),
            meta.get("medical_type", "(none)"),
        )

    pool = list(cluster_ids)
    chosen = []
    dim_counts = [Counter() for _ in range(7)]
    while pool and len(chosen) < 100:
        def score(cid):
            key = strata_key(cid)
            return (sum(dim_counts[i].get(v, 0) for i, v in enumerate(key)), stable_hash("audit", str(cid)))
        pool.sort(key=score)
        cid = pool.pop(0)
        chosen.append(cid)
        for i, v in enumerate(strata_key(cid)):
            dim_counts[i][v] += 1

    audit_rows = []
    for row_no, cid in enumerate(chosen, 1):
        res = next(r for r in cluster_rows if int(r["clinic_id"]) == cid)
        rows = treat_rows.get(cid, [])
        review_clean = [r for r in rows if r["research_status"] == "REVIEW" and r["exclusion_context"] == "NONE"]
        best = max(review_clean, key=lambda r: PROVIDER_PRIORITY.get(r["provider_context"], 0)) if review_clean else None
        meta = clinic_meta.get(cid, {})
        depts_crestix = sorted({d for d, _ in crestix_dept.get(cid, [])})
        distinct_cats = sorted({r["treatment_category_name"] for r in review_clean})
        shared = sorted(set.intersection(*[
            set(categories.get(c, {}).get("crestix_departments", [])) for c in distinct_cats
        ])) if distinct_cats else []
        guidance = (f"複数候補({' / '.join(distinct_cats)})はいずれも{'/'.join(shared) if shared else '(department不明)'}系。"
                    f"AIが「{res['final_sales_category']}」を代表カテゴリとして選択。"
                    f"医院HP({best['source_url'] if best else ''})上で実際にこの治療の実施が確認できるか確認してください。")
        audit_rows.append({
            "row_no": row_no,
            "clinic_id": cid,
            "clinic_name": meta.get("clinic_name", ""),
            "department": " / ".join(raw_dept.get(cid, [])),
            "crestix_department": " / ".join(depts_crestix),
            "candidate_treatment_categories": " / ".join(distinct_cats),
            "selected_treatment_category": res["final_sales_category"],
            "matched_alias": best["matched_alias"] if best else "",
            "provider_context": best["provider_context"] if best else "",
            "evidence_text": "",  # unavailable for this cohort (schema gap)
            "evidence_url": best["source_url"] if best else "",
            "page_title": best["page_title"] if best else "",
            "ai_decision": res["ai_decision"],
            "ai_confidence": res["ai_confidence"],
            "decision_reason": res["decision_reason"],
            "review_guidance": guidance,
            "audit_label": "",
            "audit_comment": "",
        })

    with (OUT / "same_specialty_cluster_human_audit_100.csv").open("w", encoding="utf-8-sig", newline="") as f:
        cols = list(audit_rows[0].keys())
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(audit_rows)
    print(f"PHASE 2: wrote {len(audit_rows)}-row stratified human audit sample.")

    # ================= PHASE 4-6: second pass on 287 =================
    second_pass = []
    decision_counts = Counter()
    reason_counts = Counter()
    for cid in human_review_ids:
        prior = next(r for r in human_review_rows if int(r["clinic_id"]) == cid)
        rows = treat_rows.get(cid, [])
        review_clean = [r for r in rows if r["research_status"] == "REVIEW" and r["exclusion_context"] == "NONE"]
        depts_crestix = sorted({d for d, _ in crestix_dept.get(cid, [])})
        dept_text = " ".join(raw_dept.get(cid, []))
        clinic_name = clinic_meta.get(cid, {}).get("clinic_name", "")

        if not review_clean:
            decision, confidence, reason = "HOLD", "LOW", "no_review_clean_evidence"
            final_cat = ""
        else:
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
                    contradicted.append(cat)
                    scored.append((-99, cat))
                    continue
                score = 0
                if best["provider_context"] == "PROVIDED":
                    score += 3
                elif best["provider_context"] == "POSSIBLY_PROVIDED":
                    score += 1
                if not is_generic and best["matched_alias"] == cat:
                    score += 2
                if is_generic:
                    score += 2 if context_hit else -1
                if dept_match:
                    score += 2
                scored.append((score, cat))
            scored.sort(key=lambda x: -x[0])
            top_score, top_cat = scored[0]
            runner_up = scored[1][0] if len(scored) > 1 else -99

            if contradicted and top_score <= 0:
                decision, confidence, reason = "HOLD", "LOW", "generic_alias_department_contradiction"
                final_cat = ""
            elif top_score >= 2 and top_score > runner_up:
                # Slightly lower bar than pass 1 (>=2 vs >=3): still requires a
                # strict, unambiguous winner (no tie). A lone POSSIBLY_PROVIDED
                # category (score=1) backed by nothing else still does NOT
                # qualify - that stays HUMAN_REVIEW, it is not safe to accept.
                decision, confidence, reason = "AUTO_ACCEPT", "HIGH", "second_pass_resolved"
                final_cat = top_cat
            elif len(distinct_cats) == 1 and top_score == 1 and not depts_crestix and not contradicted:
                # Single candidate, no competing category, no department data
                # to contradict it, provider_context at least POSSIBLY_PROVIDED,
                # and it is NOT a generic alias (generic aliases with no
                # context still require more than this). Weak but singular
                # and uncontradicted evidence - still only MEDIUM, stays as a
                # (much shorter) human review, not blindly accepted.
                decision, confidence, reason = "HUMAN_REVIEW", "MEDIUM", "single_weak_uncontested_candidate"
                final_cat = prior["original_sales_category"]
            else:
                decision, confidence, reason = "HUMAN_REVIEW", "MEDIUM", "still_ambiguous"
                final_cat = prior["original_sales_category"]

        decision_counts[decision] += 1
        if decision == "HUMAN_REVIEW":
            reason_counts[reason] += 1
        second_pass.append({
            "clinic_id": cid, "clinic_name": clinic_name,
            "prior_review_reason": prior["review_reason"],
            "second_pass_decision": decision, "second_pass_confidence": confidence,
            "second_pass_reason": reason, "final_sales_category": final_cat,
        })

    with (OUT / "human_review_second_pass.csv").open("w", encoding="utf-8-sig", newline="") as f:
        cols = list(second_pass[0].keys())
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(second_pass)
    print(f"PHASE 4-6: second pass on 287 -> {dict(decision_counts)}; remaining reasons: {dict(reason_counts)}")

    # ================= PHASE 7: provisional final preview =================
    with (OUT / "sales_target_classification.csv").open(encoding="utf-8-sig", newline="") as f:
        base_rows = list(csv.DictReader(f))

    resolution_by_id = {int(r["clinic_id"]): r for r in resolution_rows}
    second_pass_by_id = {r["clinic_id"]: r for r in second_pass}
    risk_by_id = {r["clinic_id"]: r for r in risk_rows}

    final_rows = []
    for r in base_rows:
        cid = int(r["clinic_id"])
        sales_tier, sales_category, treatment_category = r["sales_tier"], r["sales_category"], r["treatment_category"]
        confidence = r["confidence"]
        classification_source = "original_classification"
        human_verified = "false"
        human_review_needed = "false"
        decision_reason = r["reason"]

        if cid in resolution_by_id:
            res = resolution_by_id[cid]
            if res["ai_decision"] == "HOLD":
                sales_tier, sales_category, treatment_category = "UNKNOWN", "", ""
                confidence = "LOW"
                classification_source = "ai_resolution_hold"
                decision_reason = res["decision_reason"]
            elif res["ai_decision"] == "AUTO_ACCEPT":
                sales_tier = {"LIKELY_TREATMENT": "LIKELY_TREATMENT",
                              "SPECIALTY_TARGET": "SPECIALTY_TARGET"}.get(res["final_sales_tier"], r["sales_tier"])
                sales_category = res["final_sales_category"] or sales_category
                treatment_category = res["final_treatment_category"] or treatment_category
                confidence = "HIGH"
                classification_source = ("ai_resolution_auto_accept_same_specialty_cluster"
                                          if cid in set(cluster_ids) else "ai_resolution_auto_accept")
                decision_reason = res["decision_reason"]
                if cid in set(cluster_ids):
                    human_verified = "false"  # explicitly NOT human-audited yet (PHASE 1/2 pending)
            else:  # still HUMAN_REVIEW after pass 1 -> check second pass
                sp = second_pass_by_id.get(str(cid))
                if sp and sp["second_pass_decision"] == "AUTO_ACCEPT":
                    sales_tier = r["sales_tier"]
                    sales_category = sp["final_sales_category"] or sales_category
                    treatment_category = sp["final_sales_category"] or treatment_category
                    confidence = "HIGH"
                    classification_source = "ai_resolution_second_pass_auto_accept"
                    decision_reason = f"{sp['prior_review_reason']} -> {sp['second_pass_reason']}"
                elif sp and sp["second_pass_decision"] == "HOLD":
                    sales_tier, sales_category, treatment_category = "UNKNOWN", "", ""
                    confidence = "LOW"
                    classification_source = "ai_resolution_second_pass_hold"
                    decision_reason = f"{sp['prior_review_reason']} -> {sp['second_pass_reason']}"
                else:
                    classification_source = "ai_resolution_human_review_pending"
                    human_review_needed = "true"
                    decision_reason = res["decision_reason"]

        final_rows.append({
            "clinic_id": cid,
            "clinic_name": r["clinic_name"],
            "sales_tier": sales_tier,
            "sales_category": sales_category,
            "treatment_category": treatment_category,
            "confidence": confidence,
            "sales_usable": str(sales_tier != "UNKNOWN").lower(),
            "classification_source": classification_source,
            "human_verified": human_verified,
            "human_review_needed": human_review_needed,
            "department": r["department"],
            "crestix_department": " / ".join(sorted({d for d, _ in crestix_dept.get(cid, [])})) if cid in crestix_dept else "",
            "evidence_type": r["evidence_type"],
            "evidence_text": "",
            "evidence_url": r["evidence_url"],
            "provider_context": r["evidence_type"] if r["evidence_type"] in PROVIDER_PRIORITY else "",
            "exclusion_context": "",
            "decision_reason": decision_reason,
        })

    with (OUT / "sales_target_classification_final_preview.csv").open("w", encoding="utf-8-sig", newline="") as f:
        cols = list(final_rows[0].keys())
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(final_rows)

    tier_counts = Counter(r["sales_tier"] for r in final_rows)
    a = tier_counts.get("VERIFIED_TREATMENT", 0)
    b = tier_counts.get("LIKELY_TREATMENT", 0)
    c = tier_counts.get("SPECIALTY_TARGET", 0)
    d = tier_counts.get("UNKNOWN", 0)
    sales_targetable = a + b + c

    pending_human_review = sum(1 for r in final_rows if r["human_review_needed"] == "true")

    summary = {
        "same_specialty_cluster_total": 255,
        "same_specialty_cluster_risk_audit": dict(risk_counts),
        "human_audit_sample_prepared": 100,
        "human_audit_sample_labeled": 0,
        "phase1_human_review_input": 1070,
        "second_pass_input_287": dict(decision_counts),
        "second_pass_remaining_human_review_reasons": dict(reason_counts),
        "A_VERIFIED_TREATMENT": a,
        "B_LIKELY_TREATMENT": b,
        "C_SPECIALTY_TARGET": c,
        "D_UNKNOWN_untouched": d,
        "sales_targetable_A_plus_B_plus_C": sales_targetable,
        "sales_usability_rate_pct": round(sales_targetable / 9097 * 100, 1),
        "human_review_still_pending": pending_human_review,
        "comdesk_uuid_note": {
            "final_apply_candidates": 14363,
            "human_audit": "300/300 CORRECT (as reported by user this session; NOT independently verified or re-checked here)",
            "status": "NOT applied to Production this session; separate workstream, untouched",
        },
    }
    (OUT / "phase2_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
