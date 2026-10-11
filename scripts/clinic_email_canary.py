#!/usr/bin/env python3
"""Email Research canary; writes only local, immutable artifacts (never the DB)."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from time import monotonic

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.enrichment.email_research.formal_pipeline import research_clinic
from src.enrichment.safe_web import PinnedTransport, SafeFetcher
from src.master.email_auto_run import MeteredTransport
from src.enrichment.search_provider import SerpApiSearchProvider
from src.master.email_research_config import load_email_research_config
from src.master.email_sales_eligibility import AUTO_FALSE, AUTO_TRUE, REVIEW, may_auto_primary
from src.normalizer.address import extract_municipality


CANDIDATE_FIELDS = (
    "clinic_id", "clinic_uuid", "candidate_id", "clinic_name", "prefecture", "city", "address", "phone",
    "email", "normalized_email", "auto_decision", "confidence", "role", "ownership",
    "email_ownership", "source_owner_type",
    "verification_status", "auto_identity_decision", "clinic_name_match", "prefecture_match", "city_match",
    "discovery_source", "source", "source_url", "search_query", "verification_reason", "rejection_reason",
    "hard_block_codes", "final_sales_eligible", "primary", "rule_version", "researched_at",
)
CANDIDATE_SOURCE_FIELDS = set(CANDIDATE_FIELDS) | {
    "sales_usable", "auto_snapshot", "candidate_id_kind", "clinic_uuid_kind",
}
CLINIC_RESULT_FIELDS = (
    "clinic_id", "clinic_uuid", "clinic_name", "research_status", "candidate_found_count",
    "valid_candidate_count", "auto_true_count", "auto_false_count", "review_count", "primary_count",
    "search_query_count", "query1_called", "query2_called", "official_hp_checked",
    "official_hp_candidate_count", "navii_checked", "navii_candidate_count", "http_attempt_count",
    "retry_count", "timeout_count", "elapsed_seconds", "error",
    "stage_elapsed_official_hp", "stage_elapsed_query1", "stage_elapsed_query2",
    "stage_elapsed_verification", "official_hp_fetch_attempts", "verification_fetch_attempts",
    "search_query1_attempts", "search_query2_attempts", "search_timeout", "web_fetch_timeout",
    "retry_official_hp_count", "retry_navii_count", "retry_query1_count", "retry_query2_count",
    "source_official_hp_candidate_count", "source_navii_candidate_count",
    "source_search_query_1_candidate_count", "source_search_query_2_candidate_count",
)
COHORT_FIELDS = ("clinic_id", "clinic_uuid", "clinic_uuid_kind", "clinic_name", "prefecture", "city", "address", "phone", "hp_url", "hp_url_source")
AUDIT_FIELDS = CANDIDATE_FIELDS + ("audit_group", "human_decision", "reviewer", "review_note")


class SchemaDriftError(ValueError):
    """A producer emitted a field that is not part of the versioned artifact schema."""


def rows(path):
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def clinic_id(row):
    return str(row.get("clinic_id") or row.get("id") or "").strip()


def excluded_ids(paths):
    output = set()
    for path in paths:
        output.update(clinic_id(row) for row in rows(path))
    return output


def prepare_cohort(input_path, excludes, limit):
    excluded = excluded_ids(excludes)
    seen, selected = set(), []
    for row in rows(input_path):
        cid = clinic_id(row)
        if not cid or cid in excluded or cid in seen:
            continue
        seen.add(cid)
        raw_uuid = str(row.get("clinic_uuid") or row.get("uuid") or "").strip()
        uuid_kind = str(row.get("clinic_uuid_kind") or "").strip().upper()
        if uuid_kind not in {"REAL_UUID", "LEGACY_ID", "MISSING_UUID"}:
            uuid_kind = "REAL_UUID" if raw_uuid and not raw_uuid.startswith("legacy-id:") else "LEGACY_ID" if raw_uuid else "MISSING_UUID"
        selected.append({
            "clinic_id": int(cid), "clinic_name": row.get("clinic_name") or row.get("name") or "",
            "clinic_uuid": raw_uuid or f"legacy-id:{cid}", "clinic_uuid_kind": uuid_kind,
            "prefecture": row.get("prefecture") or "", "city": row.get("city") or extract_municipality(row.get("address") or ""),
            "address": row.get("address") or "", "phone": row.get("phone") or "", "hp_url": row.get("hp_url") or "",
            "hp_url_source": row.get("hp_url_source") or ("CLINIC_MASTER_HP_URL" if row.get("hp_url") else ""),
        })
        if len(selected) == limit:
            break
    if len(selected) != limit:
        raise SystemExit(f"non-overlapping cohort is only {len(selected)} clinics; requested {limit}")
    return selected


def write_csv(path, data, fieldnames):
    allowed = set(fieldnames)
    extras = sorted({key for row in data for key in row} - allowed)
    if extras:
        raise SchemaDriftError(f"CSV schema drift in {path.name}: unknown fields {extras}")
    with path.open("x", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="raise")
        writer.writeheader()
        for row in data:
            writer.writerow({key: row.get(key, "") for key in fieldnames})
        handle.flush()
        os.fsync(handle.fileno())


def run_one(clinic, config):
    provider = SerpApiSearchProvider(
        connect_timeout=config.connect_timeout_seconds,
        read_timeout=config.read_timeout_seconds, max_retries=config.retry_count,
    )
    fetcher = SafeFetcher(transport=MeteredTransport(PinnedTransport()), timeout=config.read_timeout_seconds)
    return research_clinic(clinic, fetcher=fetcher, search_provider=provider)


def _list_value(value):
    if isinstance(value, (list, tuple, set)):
        return ";".join(str(item) for item in value)
    return value if value is not None else ""


def _candidate_rows(result, clinic, config):
    candidates = [dict(candidate) for candidate in result.get("candidates", [])]
    for evidence in result.get("rejection_evidence", []):
        snapshot = dict(evidence.get("auto_snapshot") or {})
        candidate = dict(snapshot)
        candidate.update({
            "clinic_id": evidence.get("clinic_id", result["clinic_id"]),
            "email": snapshot.get("email") or evidence.get("normalized_email", ""),
            "discovery_source": snapshot.get("discovery_source") or evidence.get("source_type", ""),
            "source_url": snapshot.get("source_url") or evidence.get("source_url", ""),
            "auto_decision": AUTO_FALSE, "rejection_reason": evidence.get("rejection_reason", ""),
            "hard_block_codes": evidence.get("hard_block_codes", []), "final_sales_eligible": False,
        })
        candidates.append(candidate)
    for candidate in candidates:
        extras = sorted(set(candidate) - CANDIDATE_SOURCE_FIELDS)
        if extras:
            raise SchemaDriftError(f"candidate producer schema drift: unknown fields {extras}")
    primary_candidates = [c for c in candidates if may_auto_primary(c, minimum_confidence=config.primary_minimum_confidence)]
    source_rank = {"OFFICIAL_HP": 0, "NAVII": 1, "SEARCH_QUERY": 2, "SEARCH_QUERY_1": 2, "SEARCH_QUERY_2": 2, "AI_OVERVIEW": 3}
    role_rank, confidence_rank = {"GENERAL": 0, "OFFICE": 1}, {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    chosen = min(primary_candidates, key=lambda c: (
        source_rank.get(c.get("discovery_source"), 9), role_rank.get(c.get("role"), 9),
        confidence_rank.get(c.get("confidence"), 9), str(c.get("email") or "").casefold(),
    ), default=None)
    output = []
    for candidate in candidates:
        email = str(candidate.get("email") or "").strip()
        digest = hashlib.sha256(f"{result['clinic_id']}\0{email.casefold()}".encode()).hexdigest()[:20]
        decision = candidate.get("auto_decision") or candidate.get("decision") or ""
        discovery_source = candidate.get("discovery_source") or candidate.get("source") or ""
        record = {
            "clinic_id": result["clinic_id"], "clinic_uuid": clinic.get("clinic_uuid", ""),
            "candidate_id": candidate.get("candidate_id") or f"canary:{result['clinic_id']}:{digest}",
            "clinic_name": clinic.get("clinic_name", ""), "prefecture": clinic.get("prefecture", ""),
            "city": clinic.get("city", ""), "address": clinic.get("address", ""), "phone": clinic.get("phone", ""),
            "email": email, "normalized_email": candidate.get("normalized_email") or email.casefold(),
            "auto_decision": decision, "confidence": candidate.get("confidence", ""), "role": candidate.get("role", ""),
            "ownership": candidate.get("email_ownership") or candidate.get("ownership", ""),
            "email_ownership": candidate.get("email_ownership") or candidate.get("ownership", ""),
            "source_owner_type": candidate.get("source_owner_type", "UNKNOWN"),
            "verification_status": candidate.get("verification_status", ""),
            "auto_identity_decision": candidate.get("auto_identity_decision", ""),
            "clinic_name_match": candidate.get("clinic_name_match", ""),
            "prefecture_match": candidate.get("prefecture_match", ""), "city_match": candidate.get("city_match", ""),
            "discovery_source": discovery_source, "source": discovery_source,
            "source_url": candidate.get("source_url", ""), "search_query": candidate.get("search_query", ""),
            "verification_reason": candidate.get("verification_reason", ""),
            "rejection_reason": candidate.get("rejection_reason", ""),
            "hard_block_codes": _list_value(candidate.get("hard_block_codes")),
            "final_sales_eligible": candidate.get("final_sales_eligible", False), "primary": candidate is chosen,
            "rule_version": candidate.get("rule_version", ""), "researched_at": candidate.get("researched_at", ""),
        }
        output.append(record)
    return output


def make_raw_record(result, clinic, config):
    metrics = dict(result.get("metrics") or {})
    candidates = _candidate_rows(result, clinic, config)
    source_counts = dict(metrics.get("source_counts") or {})
    for key in ("OFFICIAL_HP", "NAVII", "SEARCH_QUERY_1", "SEARCH_QUERY_2"):
        source_counts.setdefault(key, 0)
    query_count = int(metrics.get("search_query_count", 0) or 0)
    retry_by_stage = dict(metrics.get("retry_by_stage") or {})
    return {
        "clinic_id": int(clinic["clinic_id"]), "clinic_uuid": clinic.get("clinic_uuid", ""),
        "clinic_uuid_kind": clinic.get("clinic_uuid_kind", "MISSING_UUID"), "clinic_name": clinic.get("clinic_name", ""),
        "research_status": result.get("research_status", "ERROR"),
        "candidate_found_count": int(metrics.get("candidate_found_count", len(candidates)) or 0),
        "valid_candidate_count": int(metrics.get("valid_candidate_count", sum(r["auto_decision"] in (AUTO_TRUE, REVIEW) for r in candidates)) or 0),
        "auto_true_count": sum(r["auto_decision"] == AUTO_TRUE for r in candidates),
        "auto_false_count": sum(r["auto_decision"] == AUTO_FALSE for r in candidates),
        "review_count": sum(r["auto_decision"] == REVIEW for r in candidates),
        "primary_count": sum(bool(r["primary"]) for r in candidates),
        "search_query_count": query_count, "query1_called": query_count >= 1, "query2_called": query_count >= 2,
        "official_hp_checked": bool(clinic.get("hp_url")),
        "official_hp_candidate_count": int(source_counts["OFFICIAL_HP"] or 0),
        # The current Canary runner has no NAVII provider; do not imply one was queried.
        "navii_checked": False, "navii_candidate_count": int(source_counts["NAVII"] or 0),
        "http_attempt_count": int(metrics.get("http_attempts", 0) or 0) + int(metrics.get("web_fetch_attempts", 0) or 0),
        "search_http_attempt_count": int(metrics.get("http_attempts", 0) or 0),
        "web_fetch_attempt_count": int(metrics.get("web_fetch_attempts", 0) or 0),
        "stage_elapsed_official_hp": float(metrics.get("stage_elapsed_official_hp", 0) or 0),
        "stage_elapsed_query1": float(metrics.get("stage_elapsed_query1", 0) or 0),
        "stage_elapsed_query2": float(metrics.get("stage_elapsed_query2", 0) or 0),
        "stage_elapsed_verification": float(metrics.get("stage_elapsed_verification", 0) or 0),
        "official_hp_fetch_attempts": int(metrics.get("official_hp_fetch_attempts", 0) or 0),
        "verification_fetch_attempts": int(metrics.get("verification_fetch_attempts", 0) or 0),
        "search_query1_attempts": int(metrics.get("search_query1_attempts", 0) or 0),
        "search_query2_attempts": int(metrics.get("search_query2_attempts", 0) or 0),
        "search_timeout": int(metrics.get("search_timeout", 0) or 0),
        "web_fetch_timeout": int(metrics.get("web_fetch_timeout", 0) or 0),
        "retry_by_stage": dict(metrics.get("retry_by_stage") or {}),
        "retry_official_hp_count": int(retry_by_stage.get("OFFICIAL_HP", 0) or 0),
        "retry_navii_count": int(retry_by_stage.get("NAVII", 0) or 0),
        "retry_query1_count": int(retry_by_stage.get("QUERY_1", 0) or 0),
        "retry_query2_count": int(retry_by_stage.get("QUERY_2", 0) or 0),
        "retry_count": int(metrics.get("retry_count", 0) or 0), "timeout_count": int(metrics.get("timeout_count", 0) or 0),
        "elapsed_seconds": float(metrics.get("elapsed_seconds", 0) or 0), "error": metrics.get("error", "") or "",
        "source_candidate_counts": source_counts, "candidates": candidates,
        "worker_duplicate": False, "search_query_over_2": query_count > 2,
    }


def clinic_csv_row(record):
    return {key: record.get(key, "") for key in CLINIC_RESULT_FIELDS}


def build_audit_rows(candidates):
    true_rows = [r for r in candidates if r.get("auto_decision") == AUTO_TRUE]
    review_rows = [r for r in candidates if r.get("auto_decision") == REVIEW]
    false_rows = [r for r in candidates if r.get("auto_decision") == AUTO_FALSE]
    rng = random.Random(20261009)
    review_sample = rng.sample(review_rows, min(len(review_rows), min(20, max(1, round(len(review_rows) * .2))))) if review_rows else []
    by_block = {}
    roles = {"RESERVATION", "RECRUIT", "RECRUITING", "MEDICAL_COOPERATION"}
    for row in sorted(false_rows, key=lambda item: item["candidate_id"]):
        codes = set(filter(None, str(row.get("hard_block_codes") or "").split(";")))
        role = str(row.get("role") or "").upper()
        for category in sorted(codes | ({role} if role in roles else set())):
            by_block.setdefault(category, row)
    chosen = {}
    for group, group_rows in (("AUTO_TRUE_ALL", true_rows), ("REVIEW_RANDOM_SAMPLE", review_sample),
                              ("AUTO_FALSE_HARD_BLOCK_SAMPLE", list(by_block.values()))):
        for row in group_rows:
            chosen[row["candidate_id"]] = (row, group)
    return [{**row, "audit_group": group, "human_decision": "", "reviewer": "", "review_note": ""}
            for row, group in chosen.values()]


def summarize_records(cohort, records, wall_seconds):
    candidates = [candidate for record in records for candidate in record.get("candidates", [])]
    counts = {decision: sum(c.get("auto_decision") == decision for c in candidates) for decision in (AUTO_TRUE, AUTO_FALSE, REVIEW)}
    uuid_values = [str(item.get("clinic_uuid") or "").strip() for item in cohort]
    real = sum(item.get("clinic_uuid_kind") == "REAL_UUID" for item in cohort)
    missing = sum(not value or item.get("clinic_uuid_kind") == "MISSING_UUID" for value, item in zip(uuid_values, cohort))
    legacy = sum(item.get("clinic_uuid_kind") == "LEGACY_ID" or value.startswith("legacy-id:") for value, item in zip(uuid_values, cohort))
    uuids = [value for value, item in zip(uuid_values, cohort) if value and item.get("clinic_uuid_kind") == "REAL_UUID"]
    duplicate_uuid_count = len(uuids) - len(set(uuids))
    total_q = sum(int(r.get("search_query_count", 0) or 0) for r in records)
    source_totals = {key: sum(int((r.get("source_candidate_counts") or {}).get(key, 0) or 0) for r in records)
                     for key in ("OFFICIAL_HP", "NAVII", "SEARCH_QUERY_1", "SEARCH_QUERY_2")}
    status_count = lambda status: sum(r.get("research_status") == status for r in records)
    review_sample = len([r for r in build_audit_rows(candidates) if r["audit_group"] == "REVIEW_RANDOM_SAMPLE"])
    return {
        "clinic_total": len(cohort), "processed": len(records), "clinic_uuid_real_count": real,
        "clinic_uuid_present_count": len(cohort) - missing, "clinic_uuid_missing_count": missing,
        "clinic_uuid_legacy_id_count": legacy, "duplicate_uuid_count": duplicate_uuid_count,
        "candidate_id_kind": "OFFLINE_SYNTHETIC", "candidate_clinic": len({r["clinic_id"] for r in candidates}),
        "candidate_count": len(candidates), "candidate_found_count": sum(r["candidate_found_count"] for r in records),
        "valid_candidate_count": sum(r["valid_candidate_count"] for r in records), **counts,
        "NO_EMAIL_FOUND": status_count("NO_EMAIL_FOUND"), "FOUND": status_count("FOUND"),
        "REVIEW_clinic_count": status_count("REVIEW"), "ERROR": status_count("ERROR"),
        "primary_count": sum(r["primary_count"] for r in records), "search_query_count": total_q,
        "search_queries_per_clinic": total_q / max(1, len(records)),
        "search_query_over_2_clinic_count": sum(bool(r.get("search_query_over_2")) for r in records),
        "query1_called": sum(bool(r.get("query1_called")) for r in records),
        "query2_called": sum(bool(r.get("query2_called")) for r in records),
        "source_candidate_counts": source_totals,
        "official_hp_checked": sum(bool(r.get("official_hp_checked")) for r in records),
        "navii_checked": sum(bool(r.get("navii_checked")) for r in records),
        "http_attempt_count": sum(r["http_attempt_count"] for r in records),
        "search_http_attempt_count": sum(r.get("search_http_attempt_count", 0) for r in records),
        "web_fetch_attempt_count": sum(r.get("web_fetch_attempt_count", 0) for r in records),
        "stage_elapsed_official_hp": sum(r.get("stage_elapsed_official_hp", 0) for r in records),
        "stage_elapsed_query1": sum(r.get("stage_elapsed_query1", 0) for r in records),
        "stage_elapsed_query2": sum(r.get("stage_elapsed_query2", 0) for r in records),
        "stage_elapsed_verification": sum(r.get("stage_elapsed_verification", 0) for r in records),
        "official_hp_fetch_attempts": sum(r.get("official_hp_fetch_attempts", 0) for r in records),
        "verification_fetch_attempts": sum(r.get("verification_fetch_attempts", 0) for r in records),
        "search_query1_attempts": sum(r.get("search_query1_attempts", 0) for r in records),
        "search_query2_attempts": sum(r.get("search_query2_attempts", 0) for r in records),
        "search_timeout": sum(r.get("search_timeout", 0) for r in records),
        "web_fetch_timeout": sum(r.get("web_fetch_timeout", 0) for r in records),
        "retry_by_stage": {stage: sum((r.get("retry_by_stage") or {}).get(stage, 0) for r in records)
                            for stage in ("OFFICIAL_HP", "NAVII", "QUERY_1", "QUERY_2")},
        "retry_count": sum(r["retry_count"] for r in records), "timeout_count": sum(r["timeout_count"] for r in records),
        "elapsed_seconds": wall_seconds, "seconds_per_clinic": wall_seconds / max(1, len(cohort)),
        "review_rate": status_count("REVIEW") / max(1, len(records)),
        "auto_true_rate": counts[AUTO_TRUE] / max(1, len(candidates)),
        "auto_false_rate": counts[AUTO_FALSE] / max(1, len(candidates)),
        "review_rate_by_candidate": counts[REVIEW] / max(1, len(candidates)),
        "worker_duplicate_count": sum(bool(r.get("worker_duplicate")) for r in records),
        "guessed_email_count": sum(bool(c.get("guessed_email") or c.get("is_guessed")) for c in candidates),
        "review_sample_count": review_sample, "human_audit_completed": False,
        "AUTO_TRUE_human_precision": None, "AUTO_TRUE_HIGH_human_precision": None,
        "AUTO_TRUE_MEDIUM_human_precision": None, "wrong_clinic_false_positive": None,
        "third_party_false_positive": None, "ownership_false_positive": None, "primary_misselection": None,
        "hard_block_primary_leakage": {code: sum(bool(c.get("primary")) and code in str(c.get("hard_block_codes", ""))
            for c in candidates) for code in ("RESERVATION", "RECRUIT", "RECRUITING", "MEDICAL_COOPERATION", "THIRD_PARTY_OWNED")},
        "production_db_write": False, "production_auto_research": False, "comdesk_write": False,
        "precision_requires_human_audit": True,
    }


def load_raw(path):
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def finalize_output(output, cohort, records, wall_seconds):
    candidates = [c for record in records for c in record.get("candidates", [])]
    write_csv(output / "candidates.csv", candidates, CANDIDATE_FIELDS)
    write_csv(output / "clinic_results.csv", [clinic_csv_row(r) for r in records], CLINIC_RESULT_FIELDS)
    write_csv(output / "human_audit_template.csv", build_audit_rows(candidates), AUDIT_FIELDS)
    summary = summarize_records(cohort, records, wall_seconds)
    with (output / "summary.json").open("x", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    return summary


def _failure_record(clinic, exc):
    return {"clinic_id": int(clinic["clinic_id"]), "clinic_uuid": clinic.get("clinic_uuid", ""),
            "clinic_uuid_kind": clinic.get("clinic_uuid_kind", "MISSING_UUID"),
            "clinic_name": clinic.get("clinic_name", ""), "research_status": "ERROR",
            "candidate_found_count": 0, "valid_candidate_count": 0, "auto_true_count": 0,
            "auto_false_count": 0, "review_count": 0, "primary_count": 0,
            "search_query_count": 0, "query1_called": False, "query2_called": False,
            "official_hp_checked": bool(clinic.get("hp_url")), "official_hp_candidate_count": 0,
            "navii_checked": False, "navii_candidate_count": 0, "http_attempt_count": 0,
            "search_http_attempt_count": 0, "web_fetch_attempt_count": 0, "retry_count": 0,
            "timeout_count": 0, "elapsed_seconds": 0, "error": str(exc),
            "stage_elapsed_official_hp": 0, "stage_elapsed_query1": 0, "stage_elapsed_query2": 0,
            "stage_elapsed_verification": 0, "official_hp_fetch_attempts": 0,
            "verification_fetch_attempts": 0, "search_query1_attempts": 0, "search_query2_attempts": 0,
            "search_timeout": 0, "web_fetch_timeout": 0,
            "retry_by_stage": {"OFFICIAL_HP": 0, "NAVII": 0, "QUERY_1": 0, "QUERY_2": 0},
            "retry_official_hp_count": 0, "retry_navii_count": 0,
            "retry_query1_count": 0, "retry_query2_count": 0,
            "source_candidate_counts": {"OFFICIAL_HP": 0, "NAVII": 0, "SEARCH_QUERY_1": 0, "SEARCH_QUERY_2": 0},
            "candidates": [], "worker_duplicate": False, "search_query_over_2": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="Read-only clinic CSV")
    parser.add_argument("--exclude", type=Path, action="append", default=[], help="Previous cohort CSV; repeatable")
    parser.add_argument("--output", type=Path, required=True, help="New artifact directory; never overwritten")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--execute", action="store_true", help="Perform public HTTP/Search calls; still no DB writes")
    parser.add_argument("--finalize-existing", action="store_true", help="Rebuild CSV/summary from output/raw_results.jsonl only")
    args = parser.parse_args(argv)
    if args.finalize_existing:
        raw_path = args.output / "raw_results.jsonl"
        if not args.output.is_dir() or not raw_path.is_file():
            raise SystemExit("finalize requires an existing output directory with raw_results.jsonl")
        cohort = rows(args.output / "cohort.csv")
        records = load_raw(raw_path)
        summary = finalize_output(args.output, cohort, records, sum(r.get("elapsed_seconds", 0) for r in records))
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    if args.input is None:
        parser.error("--input is required unless --finalize-existing is used")
    if args.output.exists():
        raise SystemExit("output path already exists; benchmark artifacts are never overwritten")
    cohort = prepare_cohort(args.input, args.exclude, args.limit)
    if not args.execute:
        print(json.dumps({"mode": "PLAN_ONLY", "clinic_count": len(cohort), "output": str(args.output),
                          "production_db_write": False}, ensure_ascii=False))
        return 0
    if not os.environ.get("SERPAPI_API_KEY"):
        raise SystemExit("SERPAPI_API_KEY is required for --execute")
    config = load_email_research_config()
    args.output.mkdir(parents=True, exist_ok=False)
    write_csv(args.output / "cohort.csv", cohort, COHORT_FIELDS)
    raw_path = args.output / "raw_results.jsonl"
    started = monotonic()
    clinic_by_id = {int(item["clinic_id"]): item for item in cohort}
    records = []
    with raw_path.open("x", encoding="utf-8") as raw_handle:
        with ThreadPoolExecutor(max_workers=config.workers) as pool:
            future_map = {pool.submit(run_one, item, config): item for item in cohort}
            for future in as_completed(future_map):
                clinic = future_map[future]
                try:
                    result = future.result()
                    record = make_raw_record({
                        "clinic_id": int(result.clinic_id), "research_status": result.research_status,
                        "candidates": [dict(c) for c in result.candidates], "rejection_evidence": list(getattr(result, "rejection_evidence", [])),
                        "metrics": dict(result.metrics),
                    }, clinic_by_id[int(result.clinic_id)], config)
                except Exception as exc:
                    record = _failure_record(clinic, exc)
                raw_handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                raw_handle.flush()
                os.fsync(raw_handle.fileno())
                records.append(record)
    wall = monotonic() - started
    summary = finalize_output(args.output, cohort, records, wall)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
