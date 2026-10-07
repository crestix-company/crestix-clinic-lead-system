#!/usr/bin/env python3
"""Stage4-D Gate2: reproducible SQLite vs Supabase READ comparator.

READ ONLY against both backends -- never writes to SQLite or Supabase (same discipline as
parity_harness.py). This is a NEW, broader harness built because no script reproducing the
"325/325 shadow/comparator" figure from docs/supabase_migration/21_stage4c_read_cutover.md
could be found in scripts/ or tests/ -- that figure reads as a one-time count from the live
async comparator in src/repository/cutover.py (logs/read_cutover.log), not a repeatable
harness. The point of this script is not to reproduce the number 325 exactly; it is to make
"same input -> SQLite result vs Supabase result -> semantic comparison -> mismatch 0" runnable
on demand, going forward.

Run: source .supabase-db.env.local && .venv/bin/python3 scripts/supabase_migration/stage4d_read_comparator.py
"""
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.repository.backend import build_repositories, BACKEND_SQLITE, BACKEND_SUPABASE
from src.repository.errors import BackendNotSupportedError
from src.master.filters import Filters
from src.master.scope import SCOPE_ALL, SCOPE_LEGACY_PRE_NATIONAL

CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


BASE = Filters(active_only=True, hp_only=False, medical_types=["医科"], prefectures=["東京都"],
                scope=SCOPE_ALL, effective_ranks=["A", "B"])

PREFECTURES = ["東京都", "大阪府", "神奈川県", "愛知県", "北海道"]
DEPARTMENTS = ["眼科", "内科", "皮膚科", "歯科", "整形外科"]
KEYWORDS = ["クリニック", "医院", "歯科", "内科", ""]

# counts: baseline + every prefecture + every department + every keyword + scope variants
for pref in PREFECTURES:
    @case(f"count_prefecture_{pref}")
    def _(repos, pref=pref):
        return repos.clinics.count(replace(BASE, prefectures=[pref]))

for dep in DEPARTMENTS:
    @case(f"count_department_{dep}")
    def _(repos, dep=dep):
        return repos.clinics.count(replace(BASE, departments=[dep]))

for kw in KEYWORDS:
    @case(f"count_keyword_{kw or 'blank'}")
    def _(repos, kw=kw):
        return repos.clinics.count(replace(BASE, keyword=kw))

for scope in (SCOPE_ALL, SCOPE_LEGACY_PRE_NATIONAL):
    @case(f"count_scope_{scope}")
    def _(repos, scope=scope):
        return repos.clinics.count(replace(BASE, scope=scope))

for uuid_mode in ("あり", "なし", "指定なし"):
    @case(f"count_uuid_mode_{uuid_mode}")
    def _(repos, uuid_mode=uuid_mode):
        return repos.clinics.count(replace(BASE, uuid_mode=uuid_mode))

for ranks in (["A"], ["B"], ["A", "B"], ["C"], ["D"], []):
    @case(f"count_effective_ranks_{'_'.join(ranks) or 'none'}")
    def _(repos, ranks=ranks):
        return repos.clinics.count(replace(BASE, effective_ranks=ranks))

# pagination: pages 0..4 (500 rows) ids must match exactly, in order
for page in range(5):
    @case(f"pagination_page_{page}_ids")
    def _(repos, page=page):
        rows = repos.clinics.query(BASE, limit=100, offset=page * 100)
        return [r["id"] for r in rows]

# funnel + metrics + dashboard-level aggregates
@case("funnel")
def _(repos):
    return repos.clinics.funnel(BASE)


@case("metrics")
def _(repos):
    return repos.clinics.metrics()


# per-row detail for a stable sample of ids (first 20 of the baseline query)
@case("detail_sample_ids")
def _(repos):
    return [r["id"] for r in repos.clinics.query(BASE, limit=20)]


# Treatment confirmed categories + status for the same sample
@case("treatment_confirmed_categories_sample")
def _(repos):
    ids = [r["id"] for r in repos.clinics.query(BASE, limit=20)]
    return [repos.treatment.confirmed_categories(cid) for cid in ids]


@case("treatment_status_for_ids_sample")
def _(repos):
    ids = [r["id"] for r in repos.clinics.query(BASE, limit=20)]
    return repos.treatment.status_for_ids(ids)


# HP rank / site type for the same sample (via get(), which includes effective_hp_rank/site_type)
@case("hp_rank_and_site_type_sample")
def _(repos):
    ids = [r["id"] for r in repos.clinics.query(BASE, limit=20)]
    return [(repos.clinics.get(cid)["effective_hp_rank"], repos.clinics.get(cid)["site_type"]) for cid in ids]


# medical_key / uuid lookups for the same sample
@case("get_by_uuid_and_medical_key_sample")
def _(repos):
    sample = repos.clinics.query(BASE, limit=20)
    out = []
    for r in sample:
        full = repos.clinics.get(r["id"])
        if full.get("uuid"):
            out.append(bool(repos.clinics.get_by_uuid(full["uuid"])))
        mk = full.get("medical_key")
        if mk:
            out.append(bool(repos.clinics.get_by_medical_key(mk)))
    return out


def run():
    sqlite_repos = build_repositories(BACKEND_SQLITE)
    supabase_repos = build_repositories(BACKEND_SUPABASE)
    matched = mismatched = errored = 0
    mismatches = []
    for name, fn in CASES:
        try:
            sqlite_value = fn(sqlite_repos)
        except BackendNotSupportedError:
            continue
        except Exception as exc:
            errored += 1
            mismatches.append((name, f"SQLITE_ERROR:{type(exc).__name__}:{exc}"))
            continue
        try:
            supabase_value = fn(supabase_repos)
        except BackendNotSupportedError:
            continue
        except Exception as exc:
            errored += 1
            mismatches.append((name, f"SUPABASE_ERROR:{type(exc).__name__}:{exc}"))
            continue
        if sqlite_value == supabase_value:
            matched += 1
        else:
            mismatched += 1
            mismatches.append((name, f"sqlite={sqlite_value!r} supabase={supabase_value!r}"))
    total = matched + mismatched + errored
    print(f"cases = {total}")
    print(f"matched = {matched}")
    print(f"mismatch = {mismatched + errored}")
    for name, detail in mismatches:
        print(f"[MISMATCH] {name}: {detail}")
    return 0 if (mismatched + errored) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(run())
