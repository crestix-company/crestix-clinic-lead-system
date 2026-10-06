#!/usr/bin/env python3
"""Stage4-A Dual Backend Parity Harness. READ ONLY against both backends -- never writes to
SQLite or Supabase. Compares SqliteClinicRepository/.../ against SupabaseClinicRepository/...
for the same inputs and asserts identical outputs.

Run: source .supabase-db.env.local && .venv/bin/python3 scripts/supabase_migration/parity_harness.py
"""
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.repository.backend import build_repositories
from src.repository.errors import BackendNotSupportedError
from src.master.filters import Filters

RESULTS = []


def scenario(name):
    def deco(fn):
        RESULTS.append((name, fn))
        return fn
    return deco


def run(sqlite_repos, supabase_repos):
    passed = failed = 0
    for name, fn in RESULTS:
        try:
            sqlite_value = fn(sqlite_repos)
        except Exception as exc:
            print(f"[ERROR][sqlite ] {name}: {type(exc).__name__}: {exc}")
            failed += 1
            continue
        try:
            supabase_value = fn(supabase_repos)
        except Exception as exc:
            print(f"[ERROR][supabase] {name}: {type(exc).__name__}: {exc}")
            failed += 1
            continue
        if sqlite_value == supabase_value:
            print(f"[PASS] {name}")
            passed += 1
        else:
            print(f"[FAIL] {name}")
            if isinstance(sqlite_value, dict) and isinstance(supabase_value, dict):
                keys = sorted(set(sqlite_value) | set(supabase_value))
                diffs = [k for k in keys if sqlite_value.get(k) != supabase_value.get(k)]
                print(f"       differing keys: {diffs}")
            else:
                print(f"       sqlite={sqlite_value!r} supabase={supabase_value!r}")
            failed += 1
    return passed, failed


# -- 1. clinic master read --------------------------------------------------
@scenario("01_get_by_id_uuid_present")
def _(r):
    rec = r.clinics.get(36519)
    return {k: rec[k] for k in ("id", "uuid", "clinic_name", "active", "hp_status")}


@scenario("02_get_by_id_uuid_blank")
def _(r):
    rec = r.clinics.get(23)
    return {k: rec[k] for k in ("id", "uuid", "active")}


@scenario("03_get_by_uuid_lookup")
def _(r):
    rec = r.clinics.get_by_uuid("0003635b-2e29-11f1-bdb5-0abc08f2747d")
    return rec["id"] if rec else None


@scenario("04_get_by_uuid_nonexistent")
def _(r):
    return r.clinics.get_by_uuid("00000000-0000-0000-0000-000000000000")


@scenario("05_get_by_medical_key_lookup")
def _(r):
    rec = r.clinics.get_by_medical_key("東京都:医科:4123055")
    return rec["id"] if rec else None


@scenario("06_get_by_medical_key_nonexistent")
def _(r):
    return r.clinics.get_by_medical_key("存在しない:医科:0000000")


# -- 2. HP ABC / effective rank / legacy fallback ---------------------------
@scenario("07_hp_rank_A_effective")
def _(r):
    rec = r.clinics.get(4)
    return (rec["machine_rank"], rec["old_hp_rank_db"], rec["effective_hp_rank"])


@scenario("08_hp_rank_B_effective")
def _(r):
    rec = r.clinics.get(6)
    return (rec["machine_rank"], rec["old_hp_rank_db"], rec["effective_hp_rank"])


@scenario("09_hp_rank_C_effective")
def _(r):
    rec = r.clinics.get(11)
    return (rec["machine_rank"], rec["old_hp_rank_db"], rec["effective_hp_rank"])


@scenario("10_hp_rank_D_effective")
def _(r):
    rec = r.clinics.get(57)
    return (rec["machine_rank"], rec["old_hp_rank_db"], rec["effective_hp_rank"])


@scenario("11_hp_rank_NO_HP_legacy_fallback")
def _(r):
    rec = r.clinics.get(81)
    return (rec["machine_rank"], rec["old_hp_rank_db"], rec["effective_hp_rank"], rec["effective_rank_reason"])


@scenario("12_hp_rank_UNKNOWN_legacy_fallback")
def _(r):
    rec = r.clinics.get(1)
    return (rec["machine_rank"], rec["old_hp_rank_db"], rec["effective_hp_rank"], rec["effective_rank_reason"])


@scenario("13_hp_research_machine_rank_direct")
def _(r):
    return r.hp_research.machine_rank(3)


@scenario("14_hp_research_batch_urls")
def _(r):
    return r.hp_research.batch_urls(3)


# -- 3. site type -------------------------------------------------------------
@scenario("15_site_type_official_verified")
def _(r):
    rec = r.clinics.get(154)
    return (rec["site_type"], rec["portal_name"])


@scenario("16_site_type_other_unknown")
def _(r):
    rec = r.clinics.get(1)
    return (rec["site_type"], rec["portal_name"])


# -- 4. Treatment --------------------------------------------------------------
@scenario("17_treatment_confirmed_categories")
def _(r):
    return r.treatment.confirmed_categories(11)


@scenario("18_treatment_confirmed_categories_empty")
def _(r):
    return r.treatment.confirmed_categories(1)


@scenario("19_treatment_status_for_ids_mixed")
def _(r):
    return r.treatment.status_for_ids([11, 4, 3, 1])


@scenario("20_website_treatment_categories")
def _(r):
    return sorted(r.hp_research.website_treatment_categories(3))


# -- 5. HP batch metrics -------------------------------------------------------
@scenario("21_hp_batch_metrics_invariants")
def _(r):
    metrics = r.hp_research.batch_metrics()
    if metrics is None:
        return None
    return (
        metrics["url_acquired"] == metrics["researched"] + metrics["failed"] + metrics["not_researched"],
        metrics["researched"] == metrics["treatment_detected"] + metrics["treatment_not_detected"],
    )


@scenario("22_hp_batch_metrics_values")
def _(r):
    return r.hp_research.batch_metrics()


# -- 6. departments / NULL / blank --------------------------------------------
@scenario("23_departments_normalized")
def _(r):
    rec = r.clinics.get(3)
    return list(rec["normalized_departments"])


@scenario("24_uuid_blank_classification")
def _(r):
    rec = r.clinics.get(23)
    return rec["uuid"] == ""


# -- 7. filters / sorting / counts --------------------------------------------
@scenario("25_count_default_filters")
def _(r):
    return r.clinics.count()


@scenario("26_query_default_filters_first_page_ids")
def _(r):
    return [rec["id"] for rec in r.clinics.query(limit=10, offset=0)]


@scenario("27_query_sorting_second_page_ids")
def _(r):
    return [rec["id"] for rec in r.clinics.query(limit=10, offset=10)]


@scenario("28_count_prefecture_filter")
def _(r):
    return r.clinics.count(Filters(active_only=False, hp_only=False, prefectures=["長野県"]))


@scenario("29_count_medical_type_filter")
def _(r):
    return r.clinics.count(Filters(active_only=False, hp_only=False, medical_types=["医科"]))


@scenario("30_count_hot_status_filter")
def _(r):
    return r.clinics.count(Filters(active_only=False, hp_only=False, hot=["かなりアツい"]))


@scenario("31_count_ranks_filter")
def _(r):
    return r.clinics.count(Filters(active_only=False, hp_only=False, ranks=["A", "B"]))


@scenario("32_count_age_min_filter")
def _(r):
    return r.clinics.count(Filters(active_only=False, hp_only=False, age_min=0.5))


@scenario("33_count_departments_filter")
def _(r):
    return r.clinics.count(Filters(active_only=False, hp_only=False, departments=["眼科"]))


@scenario("34_count_signals_filter")
def _(r):
    return r.clinics.count(Filters(active_only=False, hp_only=False, signals=["Doctors File掲載"]))


@scenario("35_count_keyword_ascii_mixed_case")
def _(r):
    return r.clinics.count(Filters(active_only=False, hp_only=False, keyword="CLINIC"))


@scenario("36_count_uuid_mode_ari")
def _(r):
    return r.clinics.count(Filters(active_only=False, hp_only=False, uuid_mode="あり"))


@scenario("37_count_uuid_mode_nashi")
def _(r):
    return r.clinics.count(Filters(active_only=False, hp_only=False, uuid_mode="なし"))


@scenario("38_count_owner_equal")
def _(r):
    return r.clinics.count(Filters(active_only=False, hp_only=False, owner_equal="一致のみ"))


@scenario("39_count_new_only")
def _(r):
    return r.clinics.count(Filters(active_only=False, hp_only=False, new_only=True))


@scenario("40_count_maps_confirmed_only")
def _(r):
    return r.clinics.count(Filters(active_only=False, hp_only=False, maps_confirmed_only=True))


@scenario("41_count_effective_ranks_post_filter")
def _(r):
    return r.clinics.count(Filters(active_only=False, hp_only=False, effective_ranks=["A", "B"]))


@scenario("42_count_site_types_post_filter")
def _(r):
    return r.clinics.count(Filters(active_only=False, hp_only=False, site_types=["OFFICIAL_VERIFIED"]))


@scenario("43_count_active_hp_only_defaults")
def _(r):
    return r.clinics.count(Filters())


@scenario("44_funnel_labels_and_counts")
def _(r):
    return r.clinics.funnel(Filters(active_only=False, hp_only=False))


@scenario("45_metrics_dict")
def _(r):
    return r.clinics.metrics()


if __name__ == "__main__":
    sqlite_repos = build_repositories("sqlite")
    supabase_repos = build_repositories("supabase")
    passed, failed = run(sqlite_repos, supabase_repos)

    # Not a parity check (the two backends are *supposed* to diverge here): confirms the
    # Supabase adapter honestly refuses fields outside Stage4-A scope instead of silently
    # computing a wrong answer.
    try:
        supabase_repos.clinics.count(Filters(active_only=False, hp_only=False, sales_tiers=["A"]))
        print("[FAIL] unsupported_filter_guard: supabase executed sales_tiers instead of refusing")
        failed += 1
    except BackendNotSupportedError:
        print("[PASS] unsupported_filter_guard: supabase refuses sales_tiers (BackendNotSupportedError)")
        passed += 1

    print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
    sys.exit(1 if failed else 0)
