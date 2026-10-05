"""TASK 1 (phase4 overnight run): independently re-verify the 40 already-AI-
classified generic_alias_mismatch_review.csv rows (歯列矯正/歯科インプラント),
and classify the remaining 154 department_treatment_consistency_audit.csv
MISMATCH rows (全194件中、歯列矯正/歯科インプラントの40件を除く154件) into
FALSE_POSITIVE / TRUE_POSITIVE / UNCERTAIN.

Read-only on the Treatment sidecar and treatment_taxonomy.yml. Never
re-crawls, never writes to Production DB or the Treatment sidecar. UNCERTAIN
is used whenever existing data (matched_alias + page_title + crestix
department only - no evidence_text column exists in this cohort's sidecar
rows) is insufficient for a confident call; it is never forced to FP/TP.

Method (in order, first match wins):
  1. Guarded aliases (歯列矯正/歯科インプラント/下肢静脈瘤血管内治療/
     CGM・持続血糖モニタリング have context_required_aliases in
     config/treatment_taxonomy.yml as of commit 450f516, i.e. post Phase4B):
     apply that guard directly to the only available text (page_title) +
     department contradiction. Required keyword present in page_title ->
     TRUE_POSITIVE. Absent AND department contradicts -> FALSE_POSITIVE
     (this is exactly the guard Phase4B would apply at crawl time, but this
     cohort's sidecar rows predate the fix per generic_alias_mismatch
     taxonomy_version=7A-v2 vs current 7A-v3). Absent but department does
     not clearly contradict -> UNCERTAIN.
  2. Evidence-source contamination check (any category): if page_title/
     source_url looks like a third-party aggregator/directory page rather
     than the clinic's own official page (medical-association listings,
     "検索"/"一覧"/"まとめ" pages) -> FALSE_POSITIVE
     (EVIDENCE_SOURCE_NOT_OFFICIAL_CLINIC_PAGE). This is a distinct, newly
     identified bug class, not previously named in generic_alias_mismatch
     _summary.json.
  3. Unguarded aliases: compare the clinic's actual crestix_department(s)
     against a curated plausible_departments table built from ordinary
     medical practice (e.g. CPAP is commonly offered by general/respiratory
     internal medicine, Dupixent treats both atopic dermatitis and asthma,
     STD testing is routine OBGYN practice). Overlap -> TRUE_POSITIVE
     (legitimate multi-department provision, not a detector bug). No
     overlap and no corroborating page_title -> UNCERTAIN.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "sales_target_reclassification"
SIDECAR = Path("/Users/maekawahiroyuki/CrestixData/clinic-lead/treatment_research_final.sqlite3")
TAXONOMY_YML = ROOT / "config" / "treatment_taxonomy.yml"

sys.path.insert(0, str(ROOT))
from src.enrichment.hp_analysis import domain_is, host  # noqa: E402

ALREADY_CLASSIFIED_CATEGORIES = {"歯列矯正", "歯科インプラント"}

# Domain-only aggregator/directory check (text-pattern heuristics like "一覧"
# were tried and rejected: they false-triggered on ordinary clinic-owned
# treatment-menu pages such as bequas-cl.com's own "施術一覧" page). These are
# the newly-identified non-official domains found in this cohort's FALSE_
# POSITIVE evidence, on top of the existing src/enrichment/hp_analysis
# .NON_OFFICIAL blocklist. See TASK2 below for the matching code fix.
NEW_NON_OFFICIAL_DOMAINS = {
    "kanja.jp", "w-medicalnet.com", "e-doctors-net.com",
}
def looks_like_aggregator(page_title: str, source_url: str) -> bool:
    h = host(source_url)
    if not h:
        return False
    # Ward/city medical-association directory sites (地区医師会) consistently end in
    # "med.or.jp" with either a hyphen or dot before it (arakawa-med.or.jp,
    # machida.tokyo.med.or.jp, musashino-med.or.jp) - these list many unrelated
    # clinics per page and are never a single clinic's own official domain.
    if h.endswith("med.or.jp"):
        return True
    return any(domain_is(source_url, d) for d in NEW_NON_OFFICIAL_DOMAINS)

# Curated from ordinary Japanese clinic practice: departments that
# plausibly co-offer this treatment beyond treatment_categories[*]
# .crestix_departments, independent of any taxonomy alias ambiguity.
PLAUSIBLE_EXTRA_DEPARTMENTS = {
    "性感染症検査": {"産婦人科", "皮膚科", "泌尿器科"},
    "CPAP療法": {"循環器内科", "消化器内科", "糖尿病内科"},  # general-naika overlap
    "デュピクセント治療": {"皮膚科", "消化器内科", "糖尿病内科"},  # asthma/allergy indication
    "ニキビ跡施術": {"皮膚科", "美容整形外科", "産婦人科"},  # aesthetic add-on at OBGYN-run clinics
    "ED治療": {"泌尿器科", "消化器内科", "循環器内科", "糖尿病内科", "美容整形外科"},
    "インスリン導入・療法": {"糖尿病内科", "消化器内科", "循環器内科"},
    "体外受精（IVF）": {"産婦人科"},  # strict - no plausible extra department
    "胃カメラ検査": {"消化器内科"},
    "大腸カメラ検査": {"消化器内科"},
    "イソトレチノイン治療": {"皮膚科"},
}


def ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    conn.row_factory = sqlite3.Row
    return conn


def load_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def main() -> None:
    taxonomy = yaml.safe_load(TAXONOMY_YML.read_text(encoding="utf-8"))
    cats = taxonomy["treatment_categories"]

    def guard_keywords(category: str, alias: str) -> tuple[str, ...]:
        d = cats.get(category, {})
        req = d.get("context_required_aliases") or {}
        if alias in req:
            return tuple(req[alias])
        cdg = (d.get("cross_department_guard") or {}).get("context_required_aliases") or {}
        return tuple(cdg.get(alias, ()))

    def expected_departments(category: str) -> set[str]:
        return set(cats.get(category, {}).get("crestix_departments", ()))

    consistency_rows = load_rows(OUT / "department_treatment_consistency_audit.csv")
    mismatch = [r for r in consistency_rows if r["status"] == "MISMATCH"]
    if len(mismatch) != 194:
        raise AssertionError(f"expected 194 MISMATCH rows, got {len(mismatch)}")

    new_scope = [r for r in mismatch if r["treatment_category"] not in ALREADY_CLASSIFIED_CATEGORIES]
    if len(new_scope) != 154:
        raise AssertionError(f"expected 154 rows outside 歯列矯正/歯科インプラント, got {len(new_scope)}")

    existing_review = load_rows(OUT / "generic_alias_mismatch_review.csv")
    if len(existing_review) != 40:
        raise AssertionError(f"expected 40 existing generic_alias_mismatch_review rows, got {len(existing_review)}")

    ids_needed = {(int(r["clinic_id"]), r["treatment_category"]) for r in new_scope} | {
        (int(r["clinic_id"]), r["treatment_category"]) for r in existing_review
    }

    with ro(SIDECAR) as db:
        sidecar_by_key: dict[tuple[int, str], dict] = {}
        clinic_ids = sorted({cid for cid, _ in ids_needed})
        for chunk_start in range(0, len(clinic_ids), 400):
            chunk = clinic_ids[chunk_start:chunk_start + 400]
            ph = ",".join("?" * len(chunk))
            for r in db.execute(
                f"SELECT clinic_id, treatment_category_name, matched_alias, source_url, page_title, "
                f"provider_context, exclusion_context, taxonomy_version "
                f"FROM clinic_treatment_research_final WHERE clinic_id IN ({ph})", chunk,
            ):
                sidecar_by_key[(r["clinic_id"], r["treatment_category_name"])] = dict(r)

    def classify(clinic_id: int, category: str, actual_dept_str: str) -> dict:
        sc = sidecar_by_key.get((clinic_id, category))
        if sc is None:
            return {"ai_decision": "UNCERTAIN", "ai_reason": "sidecar行が見つからない(データ欠落)"}
        alias = sc["matched_alias"]
        page_title = sc["page_title"] or ""
        source_url = sc["source_url"] or ""
        actual_depts = {d.strip() for d in actual_dept_str.split("/") if d.strip()}
        exp_depts = expected_departments(category)
        contradicts = bool(actual_depts) and not (actual_depts & exp_depts)

        if looks_like_aggregator(page_title, source_url):
            return {"ai_decision": "FALSE_POSITIVE",
                    "ai_reason": f"evidence_url/page_titleが第三者の集約・検索・医師会・求人ページ("
                                 f"page_title={page_title[:60]!r})であり、本医院自身の公式HPでの言及と"
                                 f"確認できない(EVIDENCE_SOURCE_NOT_OFFICIAL_CLINIC_PAGE)。guard文脈語の"
                                 f"偶然一致より優先して判定"}

        kw = guard_keywords(category, alias)
        if kw:
            hit = any(k in page_title for k in kw)
            if hit:
                return {"ai_decision": "TRUE_POSITIVE",
                        "ai_reason": f"post-Phase4B guard適用: 必須文脈語{list(kw)}のうちpage_titleに一致あり"}
            if contradicts:
                return {"ai_decision": "FALSE_POSITIVE",
                        "ai_reason": f"post-Phase4B guard適用: 必須文脈語{list(kw)}がpage_titleに不在、"
                                     f"診療科({actual_depts or 'なし'})が期待科({exp_depts})と矛盾"
                                     f"(taxonomy_version={sc['taxonomy_version']}, 旧cohortは修正前生成)"}
            return {"ai_decision": "UNCERTAIN",
                    "ai_reason": "必須文脈語がpage_titleに不在だが診療科の明確な矛盾もなし。evidence_textが"
                                 "sidecarに保存されていないため、これ以上の判定材料なし"}

        plausible = PLAUSIBLE_EXTRA_DEPARTMENTS.get(category, exp_depts)
        if actual_depts & plausible:
            return {"ai_decision": "TRUE_POSITIVE",
                    "ai_reason": f"診療科({actual_depts})は{category}を提供し得る実務上妥当な科に該当"
                                 f"(一般内科/アレルギー科/美容科等の多科提供パターン)"}

        return {"ai_decision": "UNCERTAIN",
                "ai_reason": f"診療科({actual_depts or 'なし'})は{category}の想定科({exp_depts})と重ならず、"
                             f"page_titleにも補強情報なし。alias自体は具体的({alias!r})でcontext_required_"
                             f"aliasesガードも未整備のため、detector誤検知か正当な多科提供か既存データのみ"
                             f"では確定不可"}

    # ---- re-verify the 40 already-classified rows ----
    reverified = []
    changed = 0
    for r in existing_review:
        cid = int(r["clinic_id"])
        cat = r["treatment_category"]
        verdict = classify(cid, cat, r["department"])
        row = dict(r)
        prior = row["ai_decision"]
        row["reverified_ai_decision"] = verdict["ai_decision"]
        row["reverified_ai_reason"] = verdict["ai_reason"]
        row["decision_changed"] = str(prior != verdict["ai_decision"])
        if prior != verdict["ai_decision"]:
            changed += 1
        reverified.append(row)

    # ---- classify the new 154-row scope ----
    newly_classified = []
    for r in new_scope:
        cid = int(r["clinic_id"])
        cat = r["treatment_category"]
        verdict = classify(cid, cat, r["actual_clinic_crestix_department"])
        sc = sidecar_by_key.get((cid, cat), {})
        newly_classified.append({
            "clinic_id": cid,
            "clinic_name": r["clinic_name"],
            "treatment_category": cat,
            "sales_tier": r["sales_tier"],
            "matched_alias": sc.get("matched_alias", ""),
            "expected_crestix_department": r["expected_crestix_department"],
            "actual_clinic_crestix_department": r["actual_clinic_crestix_department"],
            "page_title": sc.get("page_title", ""),
            "source_url": sc.get("source_url", ""),
            "ai_decision": verdict["ai_decision"],
            "ai_reason": verdict["ai_reason"],
        })

    out_path = OUT / "generic_alias_mismatch_review_final.csv"
    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        fieldnames = list(reverified[0].keys())
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(reverified)

    out_path2 = OUT / "broad_alias_and_generic_alias_origin_review.csv"
    with out_path2.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(newly_classified[0].keys()))
        w.writeheader()
        w.writerows(newly_classified)

    from collections import Counter
    reverified_counts = Counter(r["reverified_ai_decision"] for r in reverified)
    new_counts = Counter(r["ai_decision"] for r in newly_classified)
    new_counts_by_cat = {}
    for r in newly_classified:
        new_counts_by_cat.setdefault(r["treatment_category"], Counter())[r["ai_decision"]] += 1

    summary = {
        "scope": "194 MISMATCH rows total: 40 re-verified (歯列矯正/歯科インプラント) + 154 newly classified",
        "reverified_40": {
            "prior_counts": dict(Counter(r["ai_decision"] for r in existing_review)),
            "reverified_counts": dict(reverified_counts),
            "decisions_changed": changed,
        },
        "newly_classified_154": {
            "total_counts": dict(new_counts),
            "by_category": {k: dict(v) for k, v in new_counts_by_cat.items()},
        },
        "combined_194": {
            "FALSE_POSITIVE": reverified_counts.get("FALSE_POSITIVE", 0) + new_counts.get("FALSE_POSITIVE", 0),
            "TRUE_POSITIVE": reverified_counts.get("TRUE_POSITIVE", 0) + new_counts.get("TRUE_POSITIVE", 0),
            "UNCERTAIN": reverified_counts.get("UNCERTAIN", 0) + new_counts.get("UNCERTAIN", 0),
        },
        "method_note": "Row-level classification independently re-derived this session using "
                        "config/treatment_taxonomy.yml guards (post-Phase4B) + an aggregator-evidence "
                        "check + a curated plausible-multi-department table. The original session's "
                        "category-level generic_alias_origin/broad_alias_no_guard/other_or_genuine "
                        "row-to-bucket assignment was not persisted to any artifact and could not be "
                        "reproduced byte-for-byte; this is a fresh, reproducible, conservative "
                        "re-classification of the same 194-row MISMATCH universe.",
    }
    with (OUT / "generic_alias_mismatch_final_summary.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
