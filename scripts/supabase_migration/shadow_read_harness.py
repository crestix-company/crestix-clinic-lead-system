#!/usr/bin/env python3
"""Stage4-B Shadow Read Harness. READ ONLY against both backends -- never writes.

Independent of the live app_v2.py wiring (src/repository/shadow.py): this directly compares
SqliteClinicRepository / SupabaseClinicRepository / SupabaseTreatmentRepository (Stage4-A) over
a representative sample pulled from the real production SQLite, covering: prefecture,
medical_type, HP A/B(/C/D/NO_HP/UNKNOWN), Treatment, departments, keyword, uuid, medical_key,
pagination, sorting, NULL, blank. Never prints raw clinic rows in bulk -- only ids/counts and,
on mismatch, clinic_id + column + difference_type (same convention as reconciliation_check.py).

Run: source .supabase-db.env.local && .venv/bin/python3 scripts/supabase_migration/shadow_read_harness.py
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.repository.backend import build_repositories
from src.master.filters import Filters
from src.master.data_paths import production_db_path
from scripts.supabase_migration.full_shadow_import import ro

LATENCIES = {"sqlite": [], "supabase": []}
RESULTS = []  # (category, name, match: bool, detail)


def timed(fn):
    t0 = time.monotonic()
    value = fn()
    return value, time.monotonic() - t0


def record(category, name, sqlite_value, supabase_value, backend_sqlite_elapsed, backend_supabase_elapsed):
    LATENCIES["sqlite"].append(backend_sqlite_elapsed)
    LATENCIES["supabase"].append(backend_supabase_elapsed)
    match = sqlite_value == supabase_value
    RESULTS.append((category, name, match, None if match else (sqlite_value, supabase_value)))


def sample_ids():
    con = ro(production_db_path())
    out = {}
    out["by_rank"] = {}
    for rank in ("A", "B", "C", "D", "NO_HP", "UNKNOWN"):
        out["by_rank"][rank] = [r[0] for r in con.execute(
            "SELECT id FROM clinics WHERE hp_rank=? AND merged_into IS NULL ORDER BY id LIMIT 25", (rank,)
        )]
    out["uuid_present"] = [r[0] for r in con.execute(
        "SELECT id FROM clinics WHERE uuid<>'' AND merged_into IS NULL ORDER BY id LIMIT 15")]
    out["uuid_blank"] = [r[0] for r in con.execute(
        "SELECT id FROM clinics WHERE uuid='' AND merged_into IS NULL ORDER BY id LIMIT 15")]
    out["uuids"] = [r[0] for r in con.execute(
        "SELECT uuid FROM clinics WHERE uuid<>'' AND merged_into IS NULL ORDER BY id LIMIT 20")]
    out["medical_keys"] = [r[0] for r in con.execute(
        "SELECT medical_key FROM clinics WHERE medical_key<>'' AND merged_into IS NULL ORDER BY id LIMIT 20")]
    out["prefectures"] = [r[0] for r in con.execute("SELECT DISTINCT prefecture FROM clinics ORDER BY prefecture")]
    out["medical_types"] = [r[0] for r in con.execute("SELECT DISTINCT medical_type FROM clinics WHERE medical_type<>''")]
    out["departments"] = [json.loads(r[0])[0] for r in con.execute(
        "SELECT departments_json FROM clinics WHERE departments_json<>'[]' ORDER BY id LIMIT 20")]
    out["treatment_confirmed_ids"] = [r[0] for r in con.execute(
        "SELECT id FROM clinics WHERE hp_rank IN ('A','B','C') AND merged_into IS NULL ORDER BY id LIMIT 20")]
    out["keywords"] = [r[0][:4] for r in con.execute(
        "SELECT clinic_name FROM clinics WHERE clinic_name<>'' AND merged_into IS NULL ORDER BY id LIMIT 20")]
    con.close()
    return out


def main():
    sqlite_repos = build_repositories("sqlite")
    supabase_repos = build_repositories("supabase")
    ids = sample_ids()

    # -- clinic fetch: HP A/B/C/D/NO_HP/UNKNOWN, uuid present/blank -----------------------------
    for rank, cids in ids["by_rank"].items():
        for cid in cids:
            sv, se = timed(lambda cid=cid: sqlite_repos.clinics.get(cid))
            pv, pe = timed(lambda cid=cid: supabase_repos.clinics.get(cid))
            record(f"get_hp_rank_{rank}", cid, sv, pv, se, pe)
    for cid in ids["uuid_present"] + ids["uuid_blank"]:
        sv, se = timed(lambda cid=cid: sqlite_repos.clinics.get(cid))
        pv, pe = timed(lambda cid=cid: supabase_repos.clinics.get(cid))
        record("get_uuid_presence", cid, sv, pv, se, pe)

    # -- uuid / medical_key lookup ---------------------------------------------------------------
    for uuid in ids["uuids"]:
        sv, se = timed(lambda uuid=uuid: sqlite_repos.clinics.get_by_uuid(uuid))
        pv, pe = timed(lambda uuid=uuid: supabase_repos.clinics.get_by_uuid(uuid))
        record("get_by_uuid", uuid[:8] + "...", (sv or {}).get("id"), (pv or {}).get("id"), se, pe)
    for key in ids["medical_keys"]:
        sv, se = timed(lambda key=key: sqlite_repos.clinics.get_by_medical_key(key))
        pv, pe = timed(lambda key=key: supabase_repos.clinics.get_by_medical_key(key))
        record("get_by_medical_key", "***", (sv or {}).get("id"), (pv or {}).get("id"), se, pe)

    # -- filters: prefecture / medical_type / departments / keyword ------------------------------
    for pref in ids["prefectures"]:
        f = Filters(active_only=False, hp_only=False, prefectures=[pref])
        sv, se = timed(lambda f=f: sqlite_repos.clinics.count(f))
        pv, pe = timed(lambda f=f: supabase_repos.clinics.count(f))
        record("filter_prefecture", pref, sv, pv, se, pe)
    for mt in ids["medical_types"]:
        f = Filters(active_only=False, hp_only=False, medical_types=[mt])
        sv, se = timed(lambda f=f: sqlite_repos.clinics.count(f))
        pv, pe = timed(lambda f=f: supabase_repos.clinics.count(f))
        record("filter_medical_type", mt, sv, pv, se, pe)
    for dept in ids["departments"]:
        f = Filters(active_only=False, hp_only=False, departments=[dept])
        sv, se = timed(lambda f=f: sqlite_repos.clinics.count(f))
        pv, pe = timed(lambda f=f: supabase_repos.clinics.count(f))
        record("filter_department", dept, sv, pv, se, pe)
    for kw in ids["keywords"]:
        f = Filters(active_only=False, hp_only=False, keyword=kw)
        sv, se = timed(lambda f=f: sqlite_repos.clinics.count(f))
        pv, pe = timed(lambda f=f: supabase_repos.clinics.count(f))
        record("filter_keyword", kw, sv, pv, se, pe)
    for ranks in (["A"], ["B"], ["A", "B"], ["C"], ["D"], ["NO_HP"], ["UNKNOWN"]):
        f = Filters(active_only=False, hp_only=False, ranks=ranks)
        sv, se = timed(lambda f=f: sqlite_repos.clinics.count(f))
        pv, pe = timed(lambda f=f: supabase_repos.clinics.count(f))
        record("filter_hp_rank", ",".join(ranks), sv, pv, se, pe)
    for eff_ranks in (["A", "B"], ["D"]):
        f = Filters(active_only=False, hp_only=False, effective_ranks=eff_ranks)
        sv, se = timed(lambda f=f: sqlite_repos.clinics.count(f))
        pv, pe = timed(lambda f=f: supabase_repos.clinics.count(f))
        record("filter_effective_rank", ",".join(eff_ranks), sv, pv, se, pe)

    # -- pagination / sorting: compare full ordered id lists, not just counts --------------------
    base_filters = Filters(active_only=False, hp_only=False)
    for limit, offset in [(10, 0), (10, 10), (25, 0), (25, 25), (50, 100), (5, 500)]:
        sv, se = timed(lambda l=limit, o=offset: [r["id"] for r in sqlite_repos.clinics.query(base_filters, limit=l, offset=o)])
        pv, pe = timed(lambda l=limit, o=offset: [r["id"] for r in supabase_repos.clinics.query(base_filters, limit=l, offset=o)])
        record("pagination_sorting", f"limit={limit},offset={offset}", sv, pv, se, pe)

    # -- Treatment confirmed categories -----------------------------------------------------------
    for cid in ids["treatment_confirmed_ids"]:
        sv, se = timed(lambda cid=cid: sqlite_repos.treatment.confirmed_categories(cid))
        pv, pe = timed(lambda cid=cid: supabase_repos.treatment.confirmed_categories(cid))
        record("treatment_confirmed_categories", cid, sorted(sv), sorted(pv), se, pe)

    # -- funnel / metrics ---------------------------------------------------------------------------
    sv, se = timed(lambda: sqlite_repos.clinics.funnel(base_filters))
    pv, pe = timed(lambda: supabase_repos.clinics.funnel(base_filters))
    record("funnel", "default", sv, pv, se, pe)
    sv, se = timed(lambda: sqlite_repos.clinics.metrics())
    pv, pe = timed(lambda: supabase_repos.clinics.metrics())
    record("metrics", "default", sv, pv, se, pe)

    # -- summary -----------------------------------------------------------------------------------
    by_category = {}
    for category, name, match, detail in RESULTS:
        bucket = by_category.setdefault(category, {"match": 0, "mismatch": 0})
        bucket["match" if match else "mismatch"] += 1
        if not match:
            print(f"[MISMATCH] category={category} id={name}")
    total = len(RESULTS)
    matched = sum(1 for _, _, match, _ in RESULTS if match)
    mismatched = total - matched

    def pct(values, p):
        if not values:
            return 0.0
        s = sorted(values)
        return s[min(len(s) - 1, int(len(s) * p))]

    print(json.dumps({
        "total_scenarios": total,
        "match": matched,
        "mismatch": mismatched,
        "by_category": by_category,
        "sqlite_latency_ms": {
            "avg": round(1000 * sum(LATENCIES["sqlite"]) / len(LATENCIES["sqlite"]), 2),
            "p50": round(1000 * pct(LATENCIES["sqlite"], 0.5), 2),
            "p95": round(1000 * pct(LATENCIES["sqlite"], 0.95), 2),
        },
        "supabase_latency_ms": {
            "avg": round(1000 * sum(LATENCIES["supabase"]) / len(LATENCIES["supabase"]), 2),
            "p50": round(1000 * pct(LATENCIES["supabase"], 0.5), 2),
            "p95": round(1000 * pct(LATENCIES["supabase"], 0.95), 2),
        },
    }, indent=2, ensure_ascii=False))
    sys.exit(1 if mismatched else 0)


if __name__ == "__main__":
    main()
