"""TASK 3 (phase4 overnight run): resolve the 45 HIGH_generic_alias_or_mismatch
priority clinics from human_review_priority_subset.csv using department
evidence already present in the data (crestix_departments per candidate
category in config/treatment_taxonomy.yml). No re-crawl, no sidecar/Production
write.

Decision rule, based on which of the tied candidate categories (parsed from
the original decision_reason's candidates=[...] list) the clinic's actual
crestix_department(s) support:
  - exactly one candidate matches department AND it IS the originally
    assigned treatment_category -> AUTO_RESOLVE (confirm as-is)
  - exactly one candidate matches department but it is a DIFFERENT candidate
    -> AUTO_RESOLVE_RECLASSIFY (department evidence favors the other
       candidate; flagged separately from a plain confirm so a human can
       sanity-check before it is ever applied to Comdesk)
  - zero candidates match department -> HOLD (no departmental anchor for any
    tied candidate; likely bare alias-only evidence)
  - two or more candidates match department -> KEEP_REVIEW (still genuinely
    ambiguous even with department evidence)
The other 242 non-priority HUMAN_REVIEW clinics (human_review_second_pass.csv
minus these 45) are left untouched/maintained, per TASK3's explicit scope.
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "sales_target_reclassification"
TAXONOMY_YML = ROOT / "config" / "treatment_taxonomy.yml"

CANDIDATES_RE = re.compile(r"candidates=\[(.*?)\]")


def main() -> None:
    taxonomy = yaml.safe_load(TAXONOMY_YML.read_text(encoding="utf-8"))
    cats = taxonomy["treatment_categories"]

    def expected_departments(category: str) -> set[str]:
        return set(cats.get(category, {}).get("crestix_departments", ()))

    with (OUT / "human_review_priority_subset.csv").open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if len(rows) != 45:
        raise AssertionError(f"expected 45 priority rows, got {len(rows)}")

    with (OUT / "human_review_second_pass.csv").open(encoding="utf-8-sig", newline="") as f:
        second_pass = list(csv.DictReader(f))
    if len(second_pass) != 287:
        raise AssertionError(f"expected 287 second_pass rows, got {len(second_pass)}")
    priority_ids = {int(r["clinic_id"]) for r in rows}
    maintained = [r for r in second_pass if int(r["clinic_id"]) not in priority_ids]

    results = []
    for r in rows:
        m = CANDIDATES_RE.search(r["decision_reason"])
        candidates = [c.strip().strip("'\"") for c in m.group(1).split(",")] if m else [r["treatment_category"]]
        actual_depts = {d.strip() for d in r["department"].split("/") if d.strip()}
        matching = [c for c in candidates if actual_depts & expected_departments(c)]

        if len(matching) == 1 and matching[0] == r["treatment_category"]:
            decision, reason = "AUTO_RESOLVE", (
                f"診療科({actual_depts})は候補{candidates}中「{r['treatment_category']}」(現割当)のみを"
                f"支持。既存データの部門情報でtie-break可能"
            )
        elif len(matching) == 1:
            decision, reason = "AUTO_RESOLVE_RECLASSIFY", (
                f"診療科({actual_depts})は候補{candidates}中「{matching[0]}」のみを支持するが、"
                f"現割当は「{r['treatment_category']}」。部門根拠は別候補を支持(要確認のうえ反映)"
            )
        elif len(matching) == 0:
            decision, reason = "HOLD", (
                f"診療科({actual_depts or 'なし'})は候補{candidates}のいずれの期待科とも一致せず、"
                f"部門根拠によるtie-break不可。alias一致のみの脆弱な証拠"
            )
        else:
            decision, reason = "KEEP_REVIEW", (
                f"診療科({actual_depts})は候補{candidates}中複数({matching})を支持し、"
                f"部門根拠でも曖昧性が解消しない"
            )

        row = dict(r)
        row["candidates_parsed"] = " | ".join(candidates)
        row["department_supported_candidates"] = " | ".join(matching)
        row["resolution_decision"] = decision
        row["resolution_reason"] = reason
        results.append(row)

    out_path = OUT / "human_review_priority_resolution.csv"
    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        w.writeheader()
        w.writerows(results)

    from collections import Counter
    counts = Counter(r["resolution_decision"] for r in results)
    summary = {
        "scope": "45 HIGH_generic_alias_or_mismatch priority clinics",
        "resolution_counts": dict(counts),
        "maintained_as_is": len(maintained),
        "maintained_note": "287 human_review_second_pass clinics minus the 45 priority ones = "
                            f"{len(maintained)}; left untouched at prior Tier B/MEDIUM per TASK3 scope "
                            "(current-state note said 235 - that number predates this run and does not "
                            "reconcile against the actual 287-row file; 242 is the real remaining count).",
        "auto_resolve_reclassify_detail": [
            {"clinic_id": r["clinic_id"], "clinic_name": r["clinic_name"],
             "from": r["treatment_category"], "to": r["department_supported_candidates"]}
            for r in results if r["resolution_decision"] == "AUTO_RESOLVE_RECLASSIFY"
        ],
    }
    with (OUT / "human_review_priority_resolution_summary.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
