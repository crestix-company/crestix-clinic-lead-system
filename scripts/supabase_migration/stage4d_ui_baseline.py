#!/usr/bin/env python3
"""Stage4-D Gate2: UI baseline measurement WITHOUT launching Streamlit.

Calls the exact same Repository/Filters functions app_v2.py's simple_sales_ui() calls, with the
exact same default widget values (read directly from app_v2.py: scope=SCOPE_ALL, medical
type default "医科", prefecture default ["東京都"] when present, HP rank preset default "A+B",
all else unset). READ ONLY -- builds repositories via src.repository.backend.build_repositories()
(the live, unmodified READ path) and never writes.

Run: source .supabase-db.env.local && .venv/bin/python3 scripts/supabase_migration/stage4d_ui_baseline.py
"""
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.repository.backend import build_repositories, BACKEND_SUPABASE
from src.master.filters import Filters
from src.master.scope import SCOPE_ALL


def baseline_filters():
    # Mirrors app_v2.py:simple_sales_ui()'s status_preview_filters -> filters -> sales_filters
    # construction at its default widget state exactly (see that function for the source).
    base = Filters(
        active_only=True, hp_only=False, medical_types=["医科"], prefectures=["東京都"],
        departments=[], sales_pairs=[], scope=SCOPE_ALL,
    )
    filters = replace(base, recent_only=False, age_min=None, effective_ranks=["A", "B"],
                       site_types=[], signals=[], ad_min=0, production_companies=[], keyword="")
    return filters


def main():
    repos = build_repositories(BACKEND_SUPABASE)
    clinics = repos.clinics

    sales_filters = baseline_filters()
    results = {}
    results["営業対象"] = clinics.count(sales_filters)
    results["Comdesk出力対象"] = clinics.count(sales_filters)  # same filters as app_v2.py (export_filters = sales_filters)
    results["UUIDあり"] = clinics.count(replace(sales_filters, uuid_mode="あり"))
    results["UUIDなし"] = clinics.count(replace(sales_filters, uuid_mode="なし"))
    results["眼科"] = clinics.count(replace(sales_filters, departments=["眼科"]))
    results["keywordクリニック"] = clinics.count(replace(sales_filters, keyword="クリニック"))

    # Treatment filter: confirmed-category filter sanity check (not a baseline count target,
    # just PASS/FAIL that it executes and returns a plausible non-negative result on real data).
    treatment_ok = True
    try:
        ids = clinics.query(sales_filters, limit=5)
        for row in ids:
            repos.treatment.confirmed_categories(row["id"])
    except Exception as exc:
        treatment_ok = False
        results["Treatment_error"] = f"{type(exc).__name__}: {exc}"
    results["Treatment"] = "PASS" if treatment_ok else "FAIL"

    # Pagination: page 1 (offset 0) and page 2 (offset 100) must be disjoint and both within
    # the total count, exactly as app_v2.py's listing()/pagination controls assume.
    page1 = clinics.query(sales_filters, limit=100, offset=0)
    page2 = clinics.query(sales_filters, limit=100, offset=100)
    page1_ids = {r["id"] for r in page1}
    page2_ids = {r["id"] for r in page2}
    pagination_ok = (
        page1_ids.isdisjoint(page2_ids)
        and len(page1) == min(100, results["営業対象"])
        and (len(page2) == min(100, max(0, results["営業対象"] - 100)))
    )
    results["pagination_page2"] = "PASS" if pagination_ok else "FAIL"

    # HP A+B: effective_ranks=["A","B"] is already baked into sales_filters; confirm a
    # restricted A-only count is <= the A+B baseline (sanity, not a literal baseline target).
    a_only = clinics.count(replace(sales_filters, effective_ranks=["A"]))
    results["HP_A_leq_AB"] = "PASS" if a_only <= results["営業対象"] else "FAIL"

    print("Backend: supabase (real production data, read-only)")
    for k, v in results.items():
        print(f"{k}: {v}")

    expected = {"営業対象": 997, "Comdesk出力対象": 997, "UUIDあり": 515, "UUIDなし": 482,
                "眼科": 68, "keywordクリニック": 747}
    print("\n-- comparison against documented Stage4-C baseline (docs/supabase_migration/21_stage4c_read_cutover.md) --")
    all_match = True
    for k, expected_v in expected.items():
        actual_v = results.get(k)
        match = actual_v == expected_v
        all_match = all_match and match
        print(f"{k}: expected={expected_v} actual={actual_v} {'MATCH' if match else 'MISMATCH'}")
    print(f"\nALL MATCH: {all_match}")
    return 0 if all_match and treatment_ok and pagination_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
