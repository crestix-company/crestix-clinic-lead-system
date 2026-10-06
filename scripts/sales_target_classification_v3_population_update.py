"""v3 population update: extend the Sales Tier classification cohort to cover
clinics newly researched since the original full-run (manifest_id=
'phase7-fullrun-514bd9972b3bf20e', 9,097 clinics) that produced
sales_target_classification_final_v2_candidate.csv.

Scope (explicitly requested): population update ONLY.
  - Existing 9,097 classified clinics are copied VERBATIM from v2_candidate.
    Their tier/category/reason/human_review flags are NOT recomputed or
    changed in any way.
  - Newly researched clinics (clinic_research_status.research_status='DONE'
    under any manifest_id, not yet present in v2_candidate) are classified
    using the EXACT SAME baseline A/B/C/D rule as the original
    scripts/sales_target_reclassification.py (copied verbatim below):
      A VERIFIED_TREATMENT  - >=1 CONFIRMED treatment row
      B LIKELY_TREATMENT    - >=1 REVIEW row with exclusion_context='NONE'
      C SPECIALTY_TARGET    - no usable treatment evidence, but has an MHLW
                               crestix_department mapping (EXACT/ALIAS, or
                               REVIEW as weaker fallback), or a raw MHLW
                               department-name keyword fallback
      D UNKNOWN             - neither of the above
  - This is the BASELINE classifier only. The later AI-resolution refinement
    phases (generic-alias FP review, human-review auto-resolve, D-rescue,
    cluster audit, etc.) that were layered on top of the original cohort are
    NOT re-run here, by design ("まず母集団更新だけを行う").

Read-only on clinics.sqlite3 (Production DB), treatment_research_final.sqlite3
(Treatment Research sidecar), and mhlw_dry_run/clinic_mhlw_departments_final.sqlite3.
Never writes to any of them. Writes only a NEW artifact file
(sales_target_classification_v3_candidate.csv); the existing
sales_target_classification_final_v2_candidate.csv is never opened for writing.
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

V2_CANDIDATE = OUT / "sales_target_classification_final_v2_candidate.csv"
V3_CANDIDATE = OUT / "sales_target_classification_v3_candidate.csv"
V2_COLUMNS = [
    "clinic_id", "clinic_name", "sales_tier", "sales_category", "treatment_category",
    "confidence", "sales_usable", "classification_source", "human_verified", "human_review_needed",
    "department", "crestix_department", "evidence_type", "evidence_text", "evidence_url",
    "provider_context", "exclusion_context", "decision_reason", "cluster_final_status",
]
NEW_ROW_CLASSIFICATION_SOURCE = "v3_population_update_baseline"

# ---- baseline classifier, copied verbatim from scripts/sales_target_reclassification.py ----

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

GENERIC_ALIASES = {"インプラント", "インプラント治療", "グルー治療", "リブレ",
                    "レーザー治療", "矯正治療", "骨切り", "骨造成", "高周波治療"}

PROVIDER_PRIORITY = {"PROVIDED": 2, "POSSIBLY_PROVIDED": 1, "UNKNOWN": 0, "NOT_PROVIDED": -1}

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
    (re.compile(r"外科"), "外科一般"),
    (re.compile(r"内科"), "内科一般"),
]
RAW_DEPARTMENT_CATCHALL = {"外科一般", "内科一般"}


def classify_raw_department(names: list[str]) -> tuple[str, bool]:
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


def classify_one(cid, name, rows, depts, raw_dept_names):
    """Identical decision tree to sales_target_reclassification.py's per-clinic branch."""
    dept_names = sorted({d["crestix_department"] for d in depts})
    flag_reasons = []
    confirmed = [r for r in rows if r["research_status"] == "CONFIRMED"]
    review_clean = [r for r in rows if r["research_status"] == "REVIEW" and r["exclusion_context"] == "NONE"]

    if confirmed:
        tier = "A"
        confidence = "HIGH"
        confirmed = sorted(confirmed, key=lambda r: r["treatment_category_name"])
        primary = confirmed[0]
        sales_category = primary["treatment_category_name"]
        treatment_category = primary["treatment_category_name"]
        evidence_type = primary["provider_context"]
        evidence_url = primary["source_url"]
        reason = (f"HP上で「{primary['matched_alias']}」の提供を確認済み(research_status=CONFIRMED, "
                  f"provider_context={primary['provider_context']})。CONFIRMEDカテゴリ数={len(confirmed)}。")
    elif review_clean:
        tier = "B"
        review_clean = sorted(review_clean, key=lambda r: (-PROVIDER_PRIORITY.get(r["provider_context"], 0), r["treatment_category_name"]))
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
            if not exact_alias:
                flag_reasons.append("weak_department_mapping")
        else:
            raw_category, is_specific = classify_raw_department(raw_dept_names)
            if raw_category:
                tier = "C"
                treatment_category = ""
                sales_category = raw_category
                evidence_type = "MHLW_RAW_DEPARTMENT_TEXT"
                evidence_url = ""
                confidence = "MEDIUM" if is_specific else "LOW"
                reason = (f"MHLWの生診療科名テキスト({' / '.join(sorted(set(raw_dept_names)))})を"
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
    provider_context = evidence_type if tier in ("A", "B") else ""

    return {
        "clinic_id": cid,
        "clinic_name": name,
        "sales_tier": {"A": "VERIFIED_TREATMENT", "B": "LIKELY_TREATMENT",
                        "C": "SPECIALTY_TARGET", "D": "UNKNOWN"}[tier],
        "sales_category": sales_category,
        "treatment_category": treatment_category,
        "confidence": confidence,
        "sales_usable": str(sales_usable).lower(),
        "classification_source": NEW_ROW_CLASSIFICATION_SOURCE,
        "human_verified": "false",
        "human_review_needed": str(human_review).lower(),
        "department": " / ".join(dept_names),
        "crestix_department": "",
        "evidence_type": evidence_type,
        "evidence_text": "",
        "evidence_url": evidence_url,
        "provider_context": provider_context,
        "exclusion_context": "",
        "decision_reason": reason,
        "cluster_final_status": "",
    }, tier


def main() -> None:
    with V2_CANDIDATE.open(encoding="utf-8-sig", newline="") as f:
        existing_rows = list(csv.DictReader(f))
    existing_ids = {int(r["clinic_id"]) for r in existing_rows}

    with ro(SIDECAR) as db:
        done_ids = {r[0] for r in db.execute(
            "SELECT DISTINCT clinic_id FROM clinic_research_status WHERE research_status='DONE'")}
        new_ids = sorted(done_ids - existing_ids)

        treat_rows: dict[int, list[dict]] = {}
        for r in db.execute(
            "SELECT clinic_id, treatment_category_name, research_status, matched_alias, "
            "source_url, page_title, provider_context, exclusion_context "
            "FROM clinic_treatment_research_final"
        ):
            if r["clinic_id"] in set(new_ids):
                treat_rows.setdefault(r["clinic_id"], []).append(dict(r))

    with ro(CLINIC_DB) as db:
        clinic_name = {}
        for chunk_start in range(0, len(new_ids), 500):
            chunk = new_ids[chunk_start:chunk_start + 500]
            ph = ",".join("?" * len(chunk))
            for r in db.execute(f"SELECT id, clinic_name FROM clinics WHERE id IN ({ph})", chunk):
                clinic_name[r["id"]] = r["clinic_name"]

    with ro(MHLW_DB) as db:
        dept_rows: dict[int, list[dict]] = {}
        raw_dept_names: dict[int, list[str]] = {}
        for chunk_start in range(0, len(new_ids), 500):
            chunk = new_ids[chunk_start:chunk_start + 500]
            ph = ",".join("?" * len(chunk))
            for r in db.execute(
                f"SELECT clinic_id, mhlw_department_name, crestix_department, mapping_status "
                f"FROM clinic_mhlw_departments_final WHERE clinic_id IN ({ph}) AND mhlw_department_name<>''",
                chunk,
            ):
                raw_dept_names.setdefault(r["clinic_id"], []).append(r["mhlw_department_name"])
                if r["crestix_department"]:
                    dept_rows.setdefault(r["clinic_id"], []).append(dict(r))

    new_rows = []
    new_tier_counts = {"A": 0, "B": 0, "C": 0, "D": 0}
    for cid in new_ids:
        row, tier = classify_one(
            cid, clinic_name.get(cid, ""), treat_rows.get(cid, []),
            dept_rows.get(cid, []), raw_dept_names.get(cid, []),
        )
        new_rows.append(row)
        new_tier_counts[tier] += 1

    all_rows = existing_rows + new_rows
    with V3_CANDIDATE.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=V2_COLUMNS)
        w.writeheader()
        w.writerows(all_rows)

    existing_tier_counts = {"A": 0, "B": 0, "C": 0, "D": 0}
    tier_code = {"VERIFIED_TREATMENT": "A", "LIKELY_TREATMENT": "B", "SPECIALTY_TARGET": "C", "UNKNOWN": "D"}
    for r in existing_rows:
        existing_tier_counts[tier_code[r["sales_tier"]]] += 1

    v3_tier_counts = {k: existing_tier_counts[k] + new_tier_counts[k] for k in "ABCD"}

    with ro(CLINIC_DB) as db:
        all_v3_ids = [int(r["clinic_id"]) for r in all_rows]
        usable_ids = [int(r["clinic_id"]) for r in all_rows if r["sales_usable"] == "true"]
        ph = ",".join("?" * len(usable_ids))
        excl_sql = ("NOT (exclude_reason IN ('hospital','center') "
                    "OR COALESCE(json_extract(effective_json,'$.facility_type'),'')='病院' "
                    "OR clinic_name LIKE '%病院%' OR clinic_name LIKE '%センター%')")
        active_usable = db.execute(
            f"SELECT count(*) FROM clinics WHERE id IN ({ph}) AND merged_into IS NULL AND active=1 AND {excl_sql}",
            usable_ids,
        ).fetchone()[0]

    summary = {
        "old_cohort": len(existing_ids),
        "latest_research_cohort_done": len(done_ids),
        "newly_classified": len(new_ids),
        "new_rows_by_tier": new_tier_counts,
        "v3_total_by_tier": v3_tier_counts,
        "v3_total_cohort": len(all_rows),
        "v3_a_plus_b_plus_c": v3_tier_counts["A"] + v3_tier_counts["B"] + v3_tier_counts["C"],
        "v3_active_a_plus_b_plus_c": active_usable,
        "classification_source_for_new_rows": NEW_ROW_CLASSIFICATION_SOURCE,
        "note": "Population update only; baseline classifier applied to newly researched clinics. "
                "Existing 9,097-clinic rows copied verbatim from v2_candidate (unchanged). "
                "AI-resolution refinement phases NOT re-run on new rows by design.",
    }
    (OUT / "v3_population_update_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
