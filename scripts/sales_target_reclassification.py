"""Reclassify the original HP-Research full-run cohort (9,097 DONE clinics,
manifest_id='phase7-fullrun-514bd9972b3bf20e') into 4 sales-usability tiers,
using only data already present in the Treatment sidecar / clinic master /
MHLW department mapping. Read-only on all three. Never re-crawls, never
writes to Production DB, Treatment sidecar, or MHLW DB.

Tiers:
  A VERIFIED_TREATMENT  - >=1 CONFIRMED treatment row (existing sidecar truth)
  B LIKELY_TREATMENT    - >=1 REVIEW row with exclusion_context='NONE'
                           (i.e. identity WAS verified; ambiguity is about the
                           offer itself, not about whether this is even the
                           right clinic's page). REVIEW rows with
                           exclusion_context='IDENTITY_NOT_VERIFIED' are
                           EXCLUDED from Tier B on purpose: identity could not
                           be confirmed, so the evidence may not even belong
                           to this clinic.
  C SPECIALTY_TARGET    - no usable treatment-level evidence, but has a
                           crestix_department mapping (mapping_status
                           EXACT/ALIAS, or REVIEW as a weaker fallback) from
                           the MHLW department table.
  D UNKNOWN             - neither of the above.
"""
from __future__ import annotations

import csv
import json
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "sales_target_reclassification"
SIDECAR = Path("/Users/maekawahiroyuki/CrestixData/clinic-lead/treatment_research_final.sqlite3")
CLINIC_DB = Path("/Users/maekawahiroyuki/CrestixData/clinic-lead/clinics.sqlite3")
MHLW_DB = ROOT / "mhlw_dry_run" / "clinic_mhlw_departments_final.sqlite3"
MANIFEST = "phase7-fullrun-514bd9972b3bf20e"

GENERIC_SALES_CATEGORY = {
    "皮膚科": "皮膚科一般",
    "消化器内科": "消化器内科一般",
    "循環器内科": "循環器内科一般",
    "眼科": "眼科一般",
    "糖尿病内科": "糖尿病内科一般",
    "泌尿器科": "泌尿器科一般",
    "産婦人科": "婦人科/産科",
    "美容整形外科": "美容整形外科一般",
    "歯科": "一般歯科",
}

# Aliases the taxonomy itself already flags as needing extra context (broad /
# context_required) across any category. A Tier-B clinic resting solely on one
# of these is a weaker signal worth a human glance.
GENERIC_ALIASES = {"インプラント", "インプラント治療", "グルー治療", "リブレ",
                    "レーザー治療", "矯正治療", "骨切り", "骨造成", "高周波治療"}

PROVIDER_PRIORITY = {"PROVIDED": 2, "POSSIBLY_PROVIDED": 1, "UNKNOWN": 0, "NOT_PROVIDED": -1}

# Fallback bucketing applied directly to the RAW MHLW department-name text
# (clinic_mhlw_departments_final.mhlw_department_name) when crestix_department
# mapping is unusable (UNMAPPED/EXCLUDE/empty). The crestix field only covers
# 9 specialties; most of the cohort's raw department strings never got mapped
# into it even though the text itself is perfectly legible. Ordered
# most-specific-first; first match wins. The final two entries are broad
# catch-alls and are treated as weaker evidence (flagged for human review).
RAW_DEPARTMENT_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"歯科|矯正歯科"), "一般歯科"),
    (re.compile(r"眼科"), "眼科一般"),
    (re.compile(r"泌尿器科"), "泌尿器科一般"),
    (re.compile(r"産婦人科|婦人科|産科"), "婦人科/産科"),
    (re.compile(r"美容皮膚科|皮膚科"), "皮膚科一般"),
    (re.compile(r"整形外科"), "整形外科一般"),
    (re.compile(r"耳鼻いんこう科|耳鼻咽喉科"), "耳鼻咽喉科一般"),
    (re.compile(r"美容外科|形成外科"), "美容外科一般"),
    (re.compile(r"消化器|胃腸|内視鏡|肝臓|大腸|肛門"), "消化器科一般"),
    (re.compile(r"循環器|心臓|血管外科"), "循環器科一般"),
    (re.compile(r"糖尿病|代謝|内分泌|脂質"), "糖尿病・内分泌内科一般"),
    (re.compile(r"腎臓|人工透析"), "腎臓内科一般"),
    (re.compile(r"精神科|心療内科|神経科(?!内科|外科)"), "心療内科・精神科一般"),
    (re.compile(r"脳神経|脳外科"), "脳神経科一般"),
    (re.compile(r"アレルギ"), "アレルギー科一般"),
    (re.compile(r"リウマチ"), "リウマチ科一般"),
    (re.compile(r"リハビリテーション"), "リハビリテーション科一般"),
    (re.compile(r"乳腺"), "乳腺科一般"),
    (re.compile(r"血液|腫瘍|感染症"), "内科専門一般(血液・腫瘍・感染症)"),
    (re.compile(r"麻酔科|ペインクリニック|疼痛緩和|緩和ケア"), "麻酔科・ペインクリニック一般"),
    (re.compile(r"放射線"), "放射線科一般"),
    (re.compile(r"救急科"), "救急科一般"),
    (re.compile(r"老年"), "老年内科一般"),
    (re.compile(r"小児"), "小児科一般"),
    (re.compile(r"病理診断|臨床検査"), "病理・臨床検査科一般"),
    (re.compile(r"外科"), "外科一般"),  # broad catch-all, lower priority
    (re.compile(r"内科"), "内科一般"),  # broad catch-all, lower priority
]
RAW_DEPARTMENT_CATCHALL = {"外科一般", "内科一般"}


def classify_raw_department(names: list[str]) -> tuple[str, bool]:
    """Return (generic_sales_category, is_specific) for the strongest match
    across all of a clinic's raw MHLW department-name strings. is_specific is
    False when only a broad catch-all (外科一般/内科一般) matched."""
    matched = []
    for name in names:
        for pattern, category in RAW_DEPARTMENT_RULES:
            if pattern.search(name):
                matched.append(category)
                break
    if not matched:
        return "", False
    specific = [m for m in matched if m not in RAW_DEPARTMENT_CATCHALL]
    if specific:
        return sorted(set(specific))[0], True
    return sorted(set(matched))[0], False


def ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    conn.row_factory = sqlite3.Row
    return conn


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    with ro(SIDECAR) as db:
        cohort_ids = [r[0] for r in db.execute(
            "SELECT clinic_id FROM clinic_research_status WHERE manifest_id=? AND research_status='DONE'",
            (MANIFEST,),
        )]
        if len(cohort_ids) != 9097:
            raise AssertionError(f"cohort reconstruction changed: {len(cohort_ids)} != 9097")
        cohort_set = set(cohort_ids)

        treat_rows: dict[int, list[dict]] = {}
        for r in db.execute(
            "SELECT clinic_id, treatment_category_name, research_status, matched_alias, "
            "source_url, page_title, provider_context, exclusion_context "
            "FROM clinic_treatment_research_final"
        ):
            if r["clinic_id"] in cohort_set:
                treat_rows.setdefault(r["clinic_id"], []).append(dict(r))

    with ro(CLINIC_DB) as db:
        clinic_name = {}
        placeholders = ",".join("?" * len(cohort_ids))
        for chunk_start in range(0, len(cohort_ids), 500):
            chunk = cohort_ids[chunk_start:chunk_start + 500]
            ph = ",".join("?" * len(chunk))
            for r in db.execute(f"SELECT id, clinic_name FROM clinics WHERE id IN ({ph})", chunk):
                clinic_name[r["id"]] = r["clinic_name"]

    with ro(MHLW_DB) as db:
        dept_rows: dict[int, list[dict]] = {}
        raw_dept_names: dict[int, list[str]] = {}
        for chunk_start in range(0, len(cohort_ids), 500):
            chunk = cohort_ids[chunk_start:chunk_start + 500]
            ph = ",".join("?" * len(chunk))
            for r in db.execute(
                f"SELECT clinic_id, mhlw_department_name, crestix_department, mapping_status "
                f"FROM clinic_mhlw_departments_final WHERE clinic_id IN ({ph}) AND mhlw_department_name<>''",
                chunk,
            ):
                raw_dept_names.setdefault(r["clinic_id"], []).append(r["mhlw_department_name"])
                if r["crestix_department"]:
                    dept_rows.setdefault(r["clinic_id"], []).append(dict(r))

    rows_out = []
    human_review_count = 0
    tier_counts = {"A": 0, "B": 0, "C": 0, "D": 0}

    for cid in cohort_ids:
        name = clinic_name.get(cid, "")
        rows = treat_rows.get(cid, [])
        depts = dept_rows.get(cid, [])
        dept_names = sorted({d["crestix_department"] for d in depts})
        flag_reasons = []

        confirmed = [r for r in rows if r["research_status"] == "CONFIRMED"]
        review_clean = [r for r in rows if r["research_status"] == "REVIEW" and r["exclusion_context"] == "NONE"]

        if confirmed:
            tier = "A"
            confidence = "HIGH"
            confirmed.sort(key=lambda r: r["treatment_category_name"])
            primary = confirmed[0]
            sales_category = primary["treatment_category_name"]
            treatment_category = primary["treatment_category_name"]
            evidence_type = primary["provider_context"]
            evidence_url = primary["source_url"]
            reason = (f"HP上で「{primary['matched_alias']}」の提供を確認済み(research_status=CONFIRMED, "
                      f"provider_context={primary['provider_context']})。CONFIRMEDカテゴリ数={len(confirmed)}。")
            if len({r["treatment_category_name"] for r in confirmed}) > 1:
                pass  # multiple confirmed categories is a good outcome, not a conflict

        elif review_clean:
            tier = "B"
            review_clean.sort(key=lambda r: (-PROVIDER_PRIORITY.get(r["provider_context"], 0), r["treatment_category_name"]))
            primary = review_clean[0]
            confidence = "HIGH" if primary["provider_context"] == "PROVIDED" else "MEDIUM"
            sales_category = primary["treatment_category_name"]
            treatment_category = primary["treatment_category_name"]
            evidence_type = primary["provider_context"]
            evidence_url = primary["source_url"]
            reason = (f"識別検証済みHP上の曖昧シグナル(research_status=REVIEW, exclusion_context=NONE, "
                      f"provider_context={primary['provider_context']})。CONFIRMEDには未到達。")
            distinct_cats = {r["treatment_category_name"] for r in review_clean}
            has_provided = any(r["provider_context"] == "PROVIDED" for r in review_clean)
            if len(distinct_cats) > 1 and not has_provided:
                flag_reasons.append("category_conflict")
            if primary["matched_alias"] in GENERIC_ALIASES:
                flag_reasons.append("generic_alias")

        else:
            exact_alias = [d for d in depts if d["mapping_status"] in ("EXACT", "ALIAS")]
            review_dept = [d for d in depts if d["mapping_status"] == "REVIEW"]
            usable = exact_alias or review_dept
            if usable:
                tier = "C"
                treatment_category = ""
                pool = exact_alias or review_dept
                distinct_depts = sorted({d["crestix_department"] for d in pool})
                primary_dept = distinct_depts[0]
                sales_category = GENERIC_SALES_CATEGORY.get(primary_dept, f"{primary_dept}一般")
                evidence_type = "MHLW_DEPARTMENT_EXACT" if exact_alias else "MHLW_DEPARTMENT_REVIEW"
                evidence_url = ""
                confidence = "HIGH" if (exact_alias and len(distinct_depts) == 1) else "MEDIUM"
                reason = (f"MHLW診療科マッピング(crestix_department={primary_dept}, "
                          f"mapping_status={'EXACT/ALIAS' if exact_alias else 'REVIEW'})による診療科カテゴリ。"
                          f"治療実施の直接確認なし。")
                # NOTE: multi-department clinics (e.g. 内科+皮膚科) are normal, not a
                # conflict in themselves, so this alone no longer triggers human
                # review - only a genuinely weak (non-EXACT/ALIAS) mapping does.
                if not exact_alias:
                    flag_reasons.append("weak_department_mapping")
            else:
                raw_names = raw_dept_names.get(cid, [])
                raw_category, is_specific = classify_raw_department(raw_names)
                if raw_category:
                    tier = "C"
                    treatment_category = ""
                    sales_category = raw_category
                    evidence_type = "MHLW_RAW_DEPARTMENT_TEXT"
                    evidence_url = ""
                    confidence = "MEDIUM" if is_specific else "LOW"
                    reason = (f"MHLWの生診療科名テキスト({' / '.join(sorted(set(raw_names)))})を"
                              f"キーワード分類(crestix_department未マッピングのためフォールバック)。"
                              f"治療実施の直接確認なし。")
                    if not is_specific:
                        flag_reasons.append("generic_catchall_department")
                else:
                    tier = "D"
                    confidence = "LOW"
                    sales_category = ""
                    treatment_category = ""
                    evidence_type = "NONE"
                    evidence_url = ""
                    reason = "治療シグナルなし、かつMHLW診療科マッピングなし。公式HP情報からカテゴリを安全に決定できない。"

        sales_usable = tier != "D"
        human_review = bool(flag_reasons)
        if human_review:
            human_review_count += 1
        tier_counts[tier] += 1

        rows_out.append({
            "clinic_id": cid,
            "clinic_name": name,
            "department": " / ".join(dept_names),
            "sales_tier": {"A": "VERIFIED_TREATMENT", "B": "LIKELY_TREATMENT",
                            "C": "SPECIALTY_TARGET", "D": "UNKNOWN"}[tier],
            "sales_category": sales_category,
            "treatment_category": treatment_category,
            "confidence": confidence,
            "sales_usable": str(sales_usable).lower(),
            "evidence_type": evidence_type,
            "evidence_text": "",  # not available for this cohort; see report caveat
            "evidence_url": evidence_url,
            "reason": reason,
            "human_review_recommended": str(human_review).lower(),
            "human_review_flags": "|".join(flag_reasons),
        })

    cols = ["clinic_id", "clinic_name", "department", "sales_tier", "sales_category", "treatment_category",
            "confidence", "sales_usable", "evidence_type", "evidence_text", "evidence_url", "reason",
            "human_review_recommended", "human_review_flags"]
    with (OUT / "sales_target_classification.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows_out)

    a, b, c, d = tier_counts["A"], tier_counts["B"], tier_counts["C"], tier_counts["D"]
    total = len(rows_out)
    summary = {
        "manifest_id": MANIFEST,
        "total_cohort": total,
        "A_VERIFIED_TREATMENT": a,
        "B_LIKELY_TREATMENT": b,
        "C_SPECIALTY_TARGET": c,
        "D_UNKNOWN": d,
        "treatment_targetable_A_plus_B": a + b,
        "sales_targetable_A_plus_B_plus_C": a + b + c,
        "sales_usability_rate_pct": round((a + b + c) / total * 100, 1),
        "human_review_recommended": human_review_count,
        "human_review_pct": round(human_review_count / total * 100, 1),
        "zero_treatment_rows_clinics": sum(1 for cid in cohort_ids if not treat_rows.get(cid)),
        "no_department_mapping_at_all_clinics": sum(1 for cid in cohort_ids if not dept_rows.get(cid)),
    }
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
