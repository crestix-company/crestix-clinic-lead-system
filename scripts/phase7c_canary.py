"""Phase 7-C Canary: run the frozen Phase 7-B v3 evidence engine, unchanged, against a
fixed, stratified sample of 500 never-before-researched clinics (excluding the 270-clinic
Phase 7-B Pilot cohort). Purpose is purely to observe whether precision holds on unseen
data before any full 9,499-clinic rollout — no new judgment logic is added here.

Reuses (does not reimplement) the v3 engine: _load_inputs/_research_one_clinic from
scripts.phase7b_pilot, which in turn calls build_evidence_blocks/evaluate_treatment_evidence
unchanged. Production Clinic Master and the MHLW sidecar are opened read-only, same as Pilot.
"""
from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from scripts.phase7b_pilot import (
    CountingTransport,
    MeteredFetcher,
    _load_inputs,
    _research_one_clinic,
)
from src.enrichment.safe_web import SafeFetcher
from src.enrichment.treatment_taxonomy import EVIDENCE_ENGINE_VERSION, RULE_VERSION
from src.utils.config import ROOT

OUTPUT_DIR = ROOT / "mhlw_dry_run"
PILOT_SAMPLE_CSV = OUTPUT_DIR / "phase7b_sample_v2_focus.csv"
CANARY_SEED = "phase7c-canary-500-20260930"
TARGET_CANARY_SIZE = 500

WARD_RE = re.compile(r"東京都(.{2,4}?[区市町村郡])")


def _stable_key(*parts: object) -> str:
    return hashlib.sha256("|".join(str(p) for p in (CANARY_SEED,) + parts).encode()).hexdigest()


def _ward(address: str) -> str:
    m = WARD_RE.match(str(address or "").strip())
    return m.group(1) if m else "UNKNOWN"


def _primary_department(departments: list[str]) -> str:
    return sorted(departments)[0] if departments else "UNKNOWN"


def _pilot_clinic_ids() -> set[int]:
    if not PILOT_SAMPLE_CSV.exists():
        raise FileNotFoundError(f"Pilot sample not found, cannot exclude cohort: {PILOT_SAMPLE_CSV}")
    with PILOT_SAMPLE_CSV.open(encoding="utf-8-sig", newline="") as f:
        return {int(row["clinic_id"]) for row in csv.DictReader(f)}


def select_canary_sample(records: list[dict], target: int = TARGET_CANARY_SIZE) -> list[dict]:
    """Deterministic, stratified sample: proportional by primary crestix department,
    round-robin across wards within each department bucket for geographic spread.
    Excludes clinics already used in the Phase 7-B Pilot (and therefore Human Audit).
    """
    pilot_ids = _pilot_clinic_ids()
    pool = [r for r in records if r.get("selected_identity_url") and r["clinic_id"] not in pilot_ids]

    by_dept: dict[str, list[dict]] = defaultdict(list)
    for row in pool:
        by_dept[_primary_department(row["crestix_departments"])].append(row)

    depts_sorted = sorted(by_dept, key=lambda d: (-len(by_dept[d]), d))
    allocation = {d: round(len(by_dept[d]) / len(pool) * target) for d in depts_sorted}
    diff = target - sum(allocation.values())
    allocation[depts_sorted[0]] += diff

    selected = []
    for dept in depts_sorted:
        candidates = by_dept[dept]
        by_ward: dict[str, list[dict]] = defaultdict(list)
        for row in candidates:
            by_ward[_ward(row["address"])].append(row)
        for w in by_ward:
            by_ward[w].sort(key=lambda row: _stable_key(dept, w, row["clinic_id"]))
        wards_cycle = sorted(by_ward, key=lambda w: _stable_key(dept, w))
        chosen: list[dict] = []
        idx = 0
        n = allocation[dept]
        while len(chosen) < n and any(by_ward[w] for w in wards_cycle):
            w = wards_cycle[idx % len(wards_cycle)]
            if by_ward[w]:
                chosen.append(by_ward[w].pop(0))
            idx += 1
        selected.extend(chosen)
    return selected


def eligible_categories_for(record: dict, categories: dict) -> list[str]:
    """Department-based eligibility (same rule build_sample uses for quota computation),
    not the Pilot's legacy-signal-filtered eligible_categories (which only exists to form
    candidate-bucket strata for that sampler and is not a provision judgment)."""
    depts = set(record.get("crestix_departments", []))
    return sorted(name for name, definition in categories.items()
                  if depts.intersection(definition.get("crestix_departments", [])))


def _write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


RESULT_FIELDS = ["clinic_id", "clinic_name", "treatment_category", "research_status", "matched_alias",
                  "source_url", "page_title", "page_type", "section_heading", "provider_context",
                  "exclusion_context", "evidence_text", "fetch_status", "pages_crawled",
                  "evidence_engine_version", "rule_version", "researched_at"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=int, default=TARGET_CANARY_SIZE)
    parser.add_argument("--sample-only", action="store_true", help="write the sample list without HTTP")
    parser.add_argument("--clinic-ids", type=str, default="",
                         help="comma-separated clinic_ids to research instead of building a new sample "
                              "(used for the stability re-run subset)")
    parser.add_argument("--output-suffix", default="", help="e.g. _rerun50 for the stability check")
    args = parser.parse_args()

    records, categories = _load_inputs()
    by_id = {r["clinic_id"]: r for r in records}

    if args.clinic_ids:
        ids = [int(x) for x in args.clinic_ids.split(",") if x.strip()]
        sample_rows = [by_id[i] for i in ids]
    else:
        sample_rows = select_canary_sample(records, args.target)

    sample_csv = OUTPUT_DIR / f"phase7c_canary_500_sample{args.output_suffix}.csv"
    _write_csv(sample_csv, [
        {"clinic_id": r["clinic_id"], "clinic_name": r["clinic_name"],
         "crestix_department": ";".join(r["crestix_departments"]), "ward": _ward(r["address"]),
         "eligible_categories": ";".join(eligible_categories_for(r, categories)),
         "selected_identity_url": r["selected_identity_url"]}
        for r in sample_rows
    ], ["clinic_id", "clinic_name", "crestix_department", "ward", "eligible_categories", "selected_identity_url"])

    if args.sample_only:
        print(f"canary_sample_size={len(sample_rows)}")
        return 0

    transport = CountingTransport()
    safe_fetcher = SafeFetcher(transport=transport, timeout=10, max_bytes=2_000_000, interval=0.6)
    fetcher = MeteredFetcher(safe_fetcher)

    all_results = []
    run_meta = []
    for record in sample_rows:
        cats = eligible_categories_for(record, categories)
        if not cats:
            continue
        clinic_results, meta = _research_one_clinic(record, cats, fetcher)
        for r in clinic_results:
            all_results.append({
                "clinic_id": r["clinic_id"], "clinic_name": r["clinic_name"],
                "treatment_category": r["treatment_category"], "research_status": r["research_status"],
                "matched_alias": r["matched_alias"], "source_url": r["evidence_url"],
                "page_title": r["evidence_page_title"], "page_type": r.get("page_type", ""),
                "section_heading": r.get("section_heading", ""),
                "provider_context": r.get("provider_context", "UNKNOWN"),
                "exclusion_context": r.get("exclusion_context", "NONE"),
                "evidence_text": r["evidence_text"], "fetch_status": r["fetch_status"],
                "pages_crawled": r["fetch_successes"],
                "evidence_engine_version": r.get("evidence_engine_version", EVIDENCE_ENGINE_VERSION),
                "rule_version": r["rule_version"], "researched_at": r["researched_at"],
            })
        run_meta.append({"clinic_id": record["clinic_id"], **meta})

    results_csv = OUTPUT_DIR / f"phase7c_canary_500_results{args.output_suffix}.csv"
    _write_csv(results_csv, all_results, RESULT_FIELDS)

    status_counts = {status: sum(row["research_status"] == status for row in all_results)
                     for status in ("CONFIRMED", "REVIEW", "NOT_CONFIRMED")}
    page_attempts = sum(item["fetch_attempts"] for item in run_meta)
    page_successes = sum(item["fetch_successes"] for item in run_meta)
    summary = {
        "rule_version": RULE_VERSION, "evidence_engine_version": EVIDENCE_ENGINE_VERSION,
        "canary_seed": CANARY_SEED, "target_clinics": len(sample_rows),
        "researched_clinics": len(run_meta), "treatment_clinic_pairs": len(all_results),
        "http_page_fetch_attempts": page_attempts, "http_page_fetch_successes": page_successes,
        "http_page_fetch_success_rate": round(page_successes / page_attempts, 4) if page_attempts else 0,
        "http_request_count_including_robots_and_redirects": transport.request_count,
        "status_counts": status_counts,
        "processing_seconds": round(sum(item["processing_seconds"] for item in run_meta), 3),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    summary_json = OUTPUT_DIR / f"phase7c_canary_500_summary{args.output_suffix}.json"
    summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    import sqlite3
    db_path = OUTPUT_DIR / f"phase7c_canary_500{args.output_suffix}.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.execute("DROP TABLE IF EXISTS canary_treatment_research")
        db.execute("""CREATE TABLE canary_treatment_research (
            clinic_id INTEGER NOT NULL, treatment_category TEXT NOT NULL,
            research_status TEXT NOT NULL, matched_alias TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '', page_type TEXT NOT NULL DEFAULT '',
            exclusion_context TEXT NOT NULL DEFAULT '', rule_version TEXT NOT NULL,
            evidence_engine_version TEXT NOT NULL DEFAULT '', researched_at TEXT NOT NULL,
            PRIMARY KEY(clinic_id,treatment_category))""")
        db.executemany("INSERT INTO canary_treatment_research VALUES(?,?,?,?,?,?,?,?,?,?)",
            [(r["clinic_id"], r["treatment_category"], r["research_status"], r["matched_alias"],
              r["source_url"], r["page_type"], r["exclusion_context"], r["rule_version"],
              r["evidence_engine_version"], r["researched_at"]) for r in all_results])
        db.commit()

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
