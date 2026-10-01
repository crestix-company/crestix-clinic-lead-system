"""Phase 7-C v4 Candidate Map + Global Alias Rescue benchmark (Step 4).

Runs the SAME 500-clinic v4 Canary cohort through the new candidate_union
(department_candidates ∪ global_alias_hits) pre-filter, fetching each clinic's
official HP exactly once, then diffs the resulting CONFIRMED/REVIEW/NOT_CONFIRMED
statuses against the already-validated v4 Canary ground truth
(canary_treatment_research in the sibling worktree's phase7c_canary_500_v4_canary.sqlite3,
CONFIRMED 136 / REVIEW 628 / NOT_CONFIRMED 2249, precision 100%, PR #28).

Reuses the frozen v4 evidence engine unchanged (evaluate_treatment_evidence). Only
the CANDIDATE SELECTION differs from the original canary run:
  - baseline (that PR #28 run): department-only eligibility, using the MHLW-sidecar-
    confirmed crestix_department (Navi).
  - this run (candidate_union): department_candidates() from the clinic's own
    self-reported departments_json (no Navi/MHLW join) UNION global_alias_hits()
    (cheap scan of ALL ACTIVE43 aliases against the fetched HP text).

Purpose: prove candidate_union is a safe, Navi-independent replacement before the
9,399-clinic Full Run, where most clinics will NOT have an MHLW-confirmed
department. GO requires: all 136 baseline CONFIRMED pairs retained, 0 Major FP,
100% status parity on every pair the baseline actually evaluated, 674+ tests green.

Production Clinic Master is opened read-only. Writes nothing to Production DB or to
the shared clinic_treatment_research_final runtime DB -- this is a benchmark, not a
Research Worker run.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time

from scripts.phase7b_pilot import CountingTransport, MeteredFetcher, choose_identity_url
from src.enrichment.candidate_map import candidate_union, department_candidates, evidence_text
from src.enrichment.hp_analysis import Page, host, identity, is_official_candidate
from src.enrichment.safe_web import SafeFetcher, WebError, crawl
from src.enrichment.treatment_context import build_evidence_blocks
from src.enrichment.treatment_taxonomy import (
    EVIDENCE_ENGINE_VERSION,
    RULE_VERSION,
    active_treatment_category_names,
    evaluate_treatment_evidence,
    phase7b_research_categories,
)
from src.master.scope import LEGACY_PRE_NATIONAL_CUTOFF
from src.utils.config import ROOT

CLINIC_DB = ROOT / "data/clinics.sqlite3"
OTHER_MHLW_DIR = Path.home() / "Desktop/clinic-list-filter-complete/mhlw_dry_run"
CANARY_SAMPLE_CSV = OTHER_MHLW_DIR / "phase7c_canary_500_sample_v4_canary.csv"
CANARY_GROUND_TRUTH_DB = OTHER_MHLW_DIR / "phase7c_canary_500_v4_canary.sqlite3"

OUTPUT_DIR = ROOT / "mhlw_dry_run"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _load_canary_clinic_ids() -> list[int]:
    if not CANARY_SAMPLE_CSV.exists():
        raise FileNotFoundError(f"v4 Canary sample not found (read-only reference): {CANARY_SAMPLE_CSV}")
    with CANARY_SAMPLE_CSV.open(encoding="utf-8-sig", newline="") as f:
        return [int(row["clinic_id"]) for row in csv.DictReader(f)]


def _load_ground_truth() -> dict[tuple[int, str], str]:
    """{(clinic_id, treatment_category): research_status} from the already-validated
    PR #28 v4 Canary run -- read-only reference, not modified."""
    if not CANARY_GROUND_TRUTH_DB.exists():
        raise FileNotFoundError(f"v4 Canary ground truth not found (read-only reference): {CANARY_GROUND_TRUTH_DB}")
    db = sqlite3.connect(f"file:{CANARY_GROUND_TRUTH_DB}?mode=ro", uri=True)
    return {(row[0], row[1]): row[2]
            for row in db.execute("SELECT clinic_id, treatment_category, research_status FROM canary_treatment_research")}


def _load_records(clinic_ids: list[int]) -> dict[int, dict]:
    if not CLINIC_DB.exists():
        raise FileNotFoundError(f"Production DB not available read-only at {CLINIC_DB}")
    db = sqlite3.connect(f"file:{CLINIC_DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise ValueError(f"Production DB integrity check failed: {integrity}")
    placeholders = ",".join("?" * len(clinic_ids))
    old_results = {int(row["clinic_id"]): json.loads(row["result_json"] or "{}")
                   for row in db.execute("SELECT clinic_id, result_json FROM research_results")}
    records = {}
    for raw in db.execute(
        f"SELECT id,clinic_name,phone,address,hp_status,hp_url,maps_presence_status,maps_website_url,"
        f"departments_json,treatments_json,effective_json,base_json,first_seen_at,merged_into "
        f"FROM clinics WHERE id IN ({placeholders})", clinic_ids,
    ):
        record = dict(raw)
        record["clinic_id"] = int(record.pop("id"))
        if record["merged_into"] is not None:
            continue
        if not (record["first_seen_at"] < LEGACY_PRE_NATIONAL_CUTOFF):
            continue
        try:
            record["departments"] = json.loads(record.get("departments_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            record["departments"] = []
        old = old_results.get(record["clinic_id"], {})
        record["selected_identity_url"], record["url_source"] = choose_identity_url(record, old)
        records[record["clinic_id"]] = record
    db.close()
    return records


def _fetch_once(record: dict, fetcher) -> tuple[list, str, str, dict]:
    """Single fetch + identity-verify + small same-host crawl. Mirrors
    scripts.phase7b_pilot._research_one_clinic's fetch phase exactly, but stops
    before the per-category evaluation loop so callers can evaluate the SAME
    fetched evidence under multiple candidate-selection strategies without a
    second round of real HTTP requests."""
    start = time.monotonic()
    fetch_attempts_before = fetcher.fetch_attempts
    fetch_successes_before = fetcher.fetch_successes
    fetch_error = ""
    pages: list[Page] = []
    fetch_status = "OK"
    try:
        first = fetcher.fetch(record["selected_identity_url"])
        pages = [first]
        check = identity(record, first)
        if not check.get("verified"):
            support_links = [
                link["url"] for link in first.links
                if host(link["url"]) == host(first.url)
                and any(token in (link.get("text", "") + link["url"]).lower()
                        for token in ("アクセス", "医院概要", "医院紹介", "contact", "access", "about"))
            ]
            support = []
            for url in list(dict.fromkeys(support_links))[:2]:
                try:
                    support.append(fetcher.fetch(url, allowed_host=host(first.url)))
                except WebError as exc:
                    fetch_error = str(exc)
            if support:
                check = identity(record, Page(first.url, first.html + "\n" + "\n".join(p.html for p in support)))
                pages.extend(support)
        if not check.get("verified"):
            fetch_status = "IDENTITY_NOT_VERIFIED"
        else:
            crawled, _errors = crawl(first, fetcher, max_pages=5)
            seen = {p.url for p in pages}
            pages.extend(p for p in crawled if p.url not in seen)
    except WebError as exc:
        fetch_status = "FETCH_ERROR"
        fetch_error = str(exc)

    fetch_attempts = fetcher.fetch_attempts - fetch_attempts_before
    fetch_successes = fetcher.fetch_successes - fetch_successes_before
    official_pages = [p for p in pages if is_official_candidate(p.url)]
    evidence = [{"url": p.url, "page_title": p.title, "blocks": build_evidence_blocks(p),
                 "source_type": "OFFICIAL_HP"} for p in official_pages]
    meta = {
        "fetch_attempts": fetch_attempts, "fetch_successes": fetch_successes,
        "fetch_status": fetch_status, "error": fetch_error,
        "fetch_seconds": round(time.monotonic() - start, 3),
    }
    return evidence, fetch_status, fetch_error, meta


def _evaluate_candidates(record: dict, evidence: list, categories: set[str], checked_at: str) -> list[dict]:
    out = []
    for category in sorted(categories):
        result = evaluate_treatment_evidence(category, evidence, clinic_id=record["clinic_id"],
                                              checked_at=checked_at, clinic_name=record["clinic_name"])
        out.append({
            "clinic_id": record["clinic_id"], "treatment_category": category,
            "research_status": result["status"], "matched_alias": result.get("matched_alias", ""),
            "reason": result.get("reason", ""),
        })
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0, help="research only the first N clinics (0 = all 500)")
    parser.add_argument("--clinic-ids", type=str, default="",
                         help="comma-separated clinic_ids to re-research instead of the full 500 "
                              "(used for targeted re-verification of a fix)")
    args = parser.parse_args()

    ground_truth = _load_ground_truth()
    clinic_ids = _load_canary_clinic_ids()
    if args.clinic_ids:
        wanted = {int(x) for x in args.clinic_ids.split(",") if x.strip()}
        clinic_ids = [cid for cid in clinic_ids if cid in wanted]
    elif args.limit:
        clinic_ids = clinic_ids[: args.limit]
    records = _load_records(clinic_ids)
    active_categories = set(active_treatment_category_names())

    transport = CountingTransport()
    safe_fetcher = SafeFetcher(transport=transport, timeout=10, max_bytes=2_000_000, interval=0.6)
    fetcher = MeteredFetcher(safe_fetcher)

    run_start = time.monotonic()
    classification_seconds = 0.0
    fetch_seconds = 0.0
    all_on_results: dict[tuple[int, str], dict] = {}
    per_clinic_meta = []
    alias_only_pair_count = 0
    dept_pair_count = 0

    for idx, clinic_id in enumerate(clinic_ids, start=1):
        record = records.get(clinic_id)
        if record is None:
            continue
        evidence, fetch_status, fetch_error, fmeta = _fetch_once(record, fetcher)
        fetch_seconds += fmeta["fetch_seconds"]
        checked_at = datetime.now(timezone.utc).isoformat()

        if fetch_status != "OK":
            # Match research_worker.py's convention exactly: only a true fetch-level
            # failure is FETCH_FAILED. IDENTITY_NOT_VERIFIED means the fetch succeeded
            # (we have content) but couldn't confirm the clinic -- that is REVIEW, same
            # as the original frozen engine, and must be compared against baseline REVIEW
            # as a REVIEW, not conflated with a genuine fetch failure.
            status = "FETCH_FAILED" if fetch_status == "FETCH_ERROR" else "REVIEW"
            for category in active_categories:
                all_on_results[(clinic_id, category)] = {
                    "research_status": status, "matched_alias": "", "reason": fetch_status,
                }
            per_clinic_meta.append({"clinic_id": clinic_id, **fmeta, "candidate_count": 0})
            continue

        t0 = time.monotonic()
        dept_cands = department_candidates(record["departments"])
        text = evidence_text(evidence)
        candidates = candidate_union(record["departments"], text)
        alias_only = candidates - dept_cands
        dept_pair_count += len(dept_cands)
        alias_only_pair_count += len(alias_only)

        results = _evaluate_candidates(record, evidence, candidates, checked_at)
        classification_seconds += time.monotonic() - t0

        for r in results:
            all_on_results[(clinic_id, r["treatment_category"])] = r
        per_clinic_meta.append({"clinic_id": clinic_id, **fmeta, "candidate_count": len(candidates)})

        if idx % 25 == 0:
            print(f"...{idx}/{len(clinic_ids)} clinics processed", flush=True)

    total_seconds = round(time.monotonic() - run_start, 3)

    # --- Compare against ground truth (baseline = original v4 Canary run) ---
    baseline_pairs = {k: v for k, v in ground_truth.items() if k[0] in set(clinic_ids)}
    baseline_confirmed = {k for k, v in baseline_pairs.items() if v == "CONFIRMED"}

    retained_confirmed = sum(1 for k in baseline_confirmed
                              if all_on_results.get(k, {}).get("research_status") == "CONFIRMED")
    lost_confirmed = sorted(k for k in baseline_confirmed
                             if all_on_results.get(k, {}).get("research_status") != "CONFIRMED")

    on_confirmed = {k for k, v in all_on_results.items() if v["research_status"] == "CONFIRMED"}
    new_confirmed_not_in_baseline = sorted(k for k in on_confirmed if baseline_pairs.get(k) != "CONFIRMED")

    compared = 0
    matched = 0
    mismatches = []
    for k, base_status in baseline_pairs.items():
        on = all_on_results.get(k)
        if on is None:
            continue  # candidate_union didn't cover this pair; treated separately below
        compared += 1
        if on["research_status"] == base_status:
            matched += 1
        else:
            mismatches.append({"clinic_id": k[0], "treatment_category": k[1],
                                "baseline": base_status, "on": on["research_status"]})

    baseline_pairs_not_in_on = sorted(k for k in baseline_pairs if k not in all_on_results)

    on_status_counts = {"CONFIRMED": 0, "REVIEW": 0, "NOT_CONFIRMED": 0, "FETCH_FAILED": 0}
    for v in all_on_results.values():
        on_status_counts[v["research_status"]] = on_status_counts.get(v["research_status"], 0) + 1

    fetch_attempts = sum(m["fetch_attempts"] for m in per_clinic_meta)
    fetch_successes = sum(m["fetch_successes"] for m in per_clinic_meta)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rule_version": RULE_VERSION, "evidence_engine_version": EVIDENCE_ENGINE_VERSION,
        "clinics_requested": len(clinic_ids), "clinics_processed": len(per_clinic_meta),
        "baseline_source": str(CANARY_GROUND_TRUTH_DB),
        "baseline_pairs": len(baseline_pairs),
        "baseline_confirmed": len(baseline_confirmed),
        "on_evaluated_pairs": len(all_on_results),
        "on_candidate_pairs_evaluated_by_heavy_engine": dept_pair_count + alias_only_pair_count,
        "on_department_only_pairs": dept_pair_count,
        "on_alias_rescue_added_pairs": alias_only_pair_count,
        "retained_confirmed": retained_confirmed,
        "retained_confirmed_of_total": f"{retained_confirmed}/{len(baseline_confirmed)}",
        "lost_confirmed_pairs": lost_confirmed,
        "new_confirmed_not_in_baseline_count": len(new_confirmed_not_in_baseline),
        "new_confirmed_not_in_baseline_pairs": new_confirmed_not_in_baseline,
        "status_parity_compared_pairs": compared,
        "status_parity_matched_pairs": matched,
        "status_parity_pct": round(100 * matched / compared, 2) if compared else None,
        "status_mismatches": mismatches,
        "baseline_pairs_missing_from_on_candidate_union": baseline_pairs_not_in_on,
        "on_status_counts": on_status_counts,
        "off_status_counts_reference": {"CONFIRMED": 136, "REVIEW": 628, "NOT_CONFIRMED": 2249},
        "off_processing_seconds_reference": 2303.319,
        "off_http_fetch_attempts_reference": 2238,
        "off_http_fetch_successes_reference": 2206,
        "off_http_request_count_reference": 3308,
        "on_total_seconds": total_seconds,
        "on_fetch_seconds": round(fetch_seconds, 3),
        "on_classification_seconds": round(classification_seconds, 3),
        "on_http_fetch_attempts": fetch_attempts,
        "on_http_fetch_successes": fetch_successes,
        "on_http_request_count_including_robots_and_redirects": transport.request_count,
    }
    suffix = "_targeted" if args.clinic_ids else ""
    out_path = OUTPUT_DIR / f"phase7c_v4opt_benchmark_report{suffix}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nWrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
