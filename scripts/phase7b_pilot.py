"""Deterministic, limited Phase 7-B official-HP treatment research pilot.

The sampler uses historical treatment signals only to form candidate strata. They
are never copied to research results or passed to the treatment evaluator.
Production Clinic Master and the MHLW sidecar are opened read-only.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import random
import sqlite3
import time

from src.enrichment.hp_analysis import Page, identity, is_official_candidate, host, keyword_match, sanitize_text
from src.enrichment.safe_web import SafeFetcher, PinnedTransport, WebError, crawl
from src.enrichment.treatment_context import build_evidence_blocks
from src.enrichment.treatment_taxonomy import (
    RULE_VERSION,
    EVIDENCE_ENGINE_VERSION,
    evaluate_treatment_evidence,
    load_taxonomy,
    phase7b_research_categories,
)
from src.utils.config import ROOT

CLINIC_DB = ROOT / "data/clinics.sqlite3"
MHLW_SIDECAR = ROOT / "mhlw_dry_run/clinic_mhlw_departments_final.sqlite3"
OUTPUT_DIR = ROOT / "mhlw_dry_run"
SAMPLE_CSV = OUTPUT_DIR / "phase7b_sample.csv"
RESULTS_CSV = OUTPUT_DIR / "phase7b_results.csv"
HUMAN_AUDIT_CSV = OUTPUT_DIR / "phase7b_human_audit.csv"
SUMMARY_JSON = OUTPUT_DIR / "phase7b_summary.json"
RESULTS_DB = OUTPUT_DIR / "phase7b_treatment_research.sqlite3"
SAMPLE_SEED = "7A-v2-phase7b-pilot-20260929"
TARGET_UNIQUE_CLINICS = 270


def _suffixed(path: Path, suffix: str) -> Path:
    """Insert an --output-suffix before a path's extension, e.g. phase7b_results_v2.csv."""
    return path if not suffix else path.with_name(f"{path.stem}{suffix}{path.suffix}")


def _stable_key(category: str, clinic_id: int) -> str:
    return hashlib.sha256(f"{SAMPLE_SEED}|{category}|{clinic_id}".encode()).hexdigest()


def choose_identity_url(record: dict, old_result: dict | None = None) -> tuple[str, str]:
    """Use Maps, then saved HP URL, then saved official candidates; never search."""
    maps_url = record.get("maps_website_url", "")
    if record.get("maps_presence_status") == "MAPS_MATCHED_WEBSITE" and is_official_candidate(maps_url):
        return maps_url, "MAPS_OFFICIAL_WEBSITE"
    for field in ("hp_url", "hp_candidate_url"):
        url = str(record.get(field, "") or "").strip()
        if is_official_candidate(url):
            return url, "SAVED_HP_URL"
    old_result = old_result or {}
    url = str(old_result.get("hp_url", "") or "").strip()
    if is_official_candidate(url):
        return url, "SAVED_HP_RESEARCH_URL"
    candidates = old_result.get("hp_candidates", [])
    if isinstance(candidates, list):
        for candidate in candidates:
            url = str(candidate.get("url", "") if isinstance(candidate, dict) else candidate).strip()
            if is_official_candidate(url):
                return url, "SAVED_OFFICIAL_CANDIDATE"
    return "", "NO_OFFICIAL_URL"


def _legacy_candidate_signal(record: dict, old_result: dict, definition: dict) -> bool:
    """Historical signals only form a sampling bucket; they are not labels."""
    terms = [str(value) for value in definition.get("source_sales_items", [])]
    terms += [str(value) for value in definition.get("aliases", [])]
    terms = list(dict.fromkeys(value for value in terms if value))
    legacy_categories = record.get("treatments_json", [])
    if isinstance(legacy_categories, str):
        try:
            legacy_categories = json.loads(legacy_categories)
        except (json.JSONDecodeError, TypeError):
            legacy_categories = []
    if any(keyword_match(term, str(item)) for term in terms for item in legacy_categories):
        return True
    evidence_rows = old_result.get("treatment_evidence", [])
    for evidence in evidence_rows if isinstance(evidence_rows, list) else []:
        if not isinstance(evidence, dict):
            continue
        evidence_url = str(evidence.get("evidence_url", evidence.get("url", "")))
        if evidence_url and not is_official_candidate(evidence_url):
            continue
        corpus = " ".join(str(evidence.get(key, "")) for key in ("keyword", "label", "category", "evidence", "text"))
        if any(keyword_match(term, corpus) for term in terms):
            return True
    return False


def build_sample(records: list[dict], categories: dict, target: int = TARGET_UNIQUE_CLINICS) -> tuple[list[dict], dict]:
    """Stratified deterministic clinic × category sample with truthful shortages."""
    by_pair: dict[tuple[int, str], dict] = {}
    eligible_by_category: dict[str, list[dict]] = {}
    for category, definition in categories.items():
        depts = set(definition.get("crestix_departments", []))
        eligible_by_category[category] = [
            row for row in records
            if depts.intersection(row.get("crestix_departments", [])) and row.get("selected_identity_url")
        ]
        eligible = eligible_by_category[category]
        positives = sorted((r for r in eligible if r.get("legacy_signal", {}).get(category)),
                           key=lambda r: _stable_key(category, r["clinic_id"]))
        negatives = sorted((r for r in eligible if not r.get("legacy_signal", {}).get(category)),
                           key=lambda r: _stable_key(category, r["clinic_id"]))
        for bucket, source, quota in (("POSITIVE_CANDIDATE", positives, 2), ("NEGATIVE_CANDIDATE", negatives, 4)):
            for row in source[:quota]:
                by_pair.setdefault((row["clinic_id"], category), {
                    "clinic_id": row["clinic_id"], "treatment_category": category,
                    "candidate_bucket": bucket,
                })

    # Preserve category quotas first, then top up unique clinics to the 270 target.
    selected_ids = {clinic_id for clinic_id, _ in by_pair}
    all_records = {row["clinic_id"]: row for row in records if row.get("selected_identity_url")}
    strata_order = ("verified", "large", "small", "multi", "maps", "remaining")

    def in_stratum(row: dict, stratum: str) -> bool:
        return {
            "verified": bool(row.get("hp_identity_verified")),
            "large": int(row.get("prior_hp_page_count", 0)) >= 4,
            "small": int(row.get("prior_hp_page_count", 0)) <= 1,
            "multi": len(row.get("crestix_departments", [])) > 1,
            "maps": row.get("url_source") == "MAPS_OFFICIAL_WEBSITE",
            "remaining": True,
        }[stratum]

    # Approximately balanced replenishment across available design strata.
    while len(selected_ids) < target:
        progressed = False
        for stratum in strata_order:
            choices = sorted(
                (row for cid, row in all_records.items() if cid not in selected_ids and in_stratum(row, stratum)
                 and row.get("crestix_departments")),
                key=lambda row: hashlib.sha256(f"{SAMPLE_SEED}|{stratum}|{row['clinic_id']}".encode()).hexdigest(),
            )
            if not choices:
                continue
            row = choices[0]
            selected_ids.add(row["clinic_id"])
            for category in row.get("eligible_categories", []):
                key = (row["clinic_id"], category)
                if key not in by_pair:
                    by_pair[key] = {
                        "clinic_id": row["clinic_id"], "treatment_category": category,
                        "candidate_bucket": "POSITIVE_CANDIDATE" if row.get("legacy_signal", {}).get(category)
                        else "NEGATIVE_CANDIDATE",
                    }
            progressed = True
            if len(selected_ids) >= target:
                break
        if not progressed:
            break

    sample_rows = []
    for key, pair in sorted(by_pair.items(), key=lambda item: (item[0][0], item[0][1])):
        record = all_records[key[0]]
        sample_rows.append({
            "clinic_id": record["clinic_id"], "clinic_name": record["clinic_name"],
            "crestix_department": ";".join(record.get("crestix_departments", [])),
            "treatment_category": pair["treatment_category"],
            "candidate_bucket": pair["candidate_bucket"],
            "selected_identity_url": record["selected_identity_url"],
            "url_source": record["url_source"],
        })
    quotas = {}
    for category in categories:
        rows = [r for r in sample_rows if r["treatment_category"] == category]
        quotas[category] = {
            "eligible_clinics": len(eligible_by_category[category]),
            "positive_candidate_rows": sum(r["candidate_bucket"] == "POSITIVE_CANDIDATE" for r in rows),
            "negative_candidate_rows": sum(r["candidate_bucket"] == "NEGATIVE_CANDIDATE" for r in rows),
            "minimum_positive_target": 2,
            "minimum_negative_target": 4,
        }
    unique = len(selected_ids)
    if unique < min(target, 200):
        raise ValueError(f"insufficient eligible official-HP sample: {unique} clinics")
    return sample_rows, {"unique_clinics": unique, "category_candidate_quotas": quotas}


def _load_inputs() -> tuple[list[dict], dict]:
    if not CLINIC_DB.exists() or not MHLW_SIDECAR.exists():
        raise FileNotFoundError("Production Clinic Master or final MHLW sidecar is unavailable")
    clinic = sqlite3.connect(f"file:{CLINIC_DB}?mode=ro", uri=True)
    clinic.row_factory = sqlite3.Row
    sidecar = sqlite3.connect(f"file:{MHLW_SIDECAR}?mode=ro", uri=True)
    sidecar.row_factory = sqlite3.Row
    try:
        integrity = clinic.execute("PRAGMA integrity_check").fetchone()[0]
        count = clinic.execute("SELECT COUNT(*) FROM clinics").fetchone()[0]
        if count != 13970 or integrity != "ok":
            raise ValueError(f"Clinic Master baseline mismatch: count={count}, integrity={integrity}")
        sidecar_integrity = sidecar.execute("PRAGMA integrity_check").fetchone()[0]
        sidecar_count = sidecar.execute("SELECT COUNT(DISTINCT clinic_id) FROM clinic_mhlw_departments_final").fetchone()[0]
        if sidecar_integrity != "ok" or sidecar_count != 9830:
            raise ValueError(f"final sidecar mismatch: clinics={sidecar_count}, integrity={sidecar_integrity}")

        crestix_by_clinic: dict[int, set[str]] = {}
        for row in sidecar.execute("SELECT DISTINCT clinic_id, crestix_department FROM clinic_mhlw_departments_final WHERE crestix_department<>''"):
            crestix_by_clinic.setdefault(int(row["clinic_id"]), set()).add(row["crestix_department"])
        old_results = {int(row["clinic_id"]): json.loads(row["result_json"] or "{}")
                       for row in clinic.execute("SELECT clinic_id, result_json FROM research_results")}
        records = []
        categories = phase7b_research_categories()
        for raw in clinic.execute("SELECT id,clinic_name,phone,address,hp_status,hp_url,maps_presence_status,maps_website_url,treatments_json,effective_json,base_json FROM clinics WHERE merged_into IS NULL ORDER BY id"):
            record = dict(raw)
            record["clinic_id"] = int(record.pop("id"))
            record["crestix_departments"] = sorted(crestix_by_clinic.get(record["clinic_id"], set()))
            if not record["crestix_departments"]:
                continue
            old = old_results.get(record["clinic_id"], {})
            for field in ("effective_json", "base_json"):
                try:
                    payload = json.loads(record[field] or "{}")
                except (json.JSONDecodeError, TypeError):
                    payload = {}
                if isinstance(payload, dict):
                    for key in ("hp_candidate_url", "hp_url"):
                        if not record.get(key) and payload.get(key):
                            record[key] = payload[key]
            record["selected_identity_url"], record["url_source"] = choose_identity_url(record, old)
            record["hp_identity_verified"] = record.get("hp_status") == "VERIFIED" or bool(old.get("hp_verified"))
            record["prior_hp_page_count"] = len(old.get("hp_identity_pages", [])) if isinstance(old.get("hp_identity_pages"), list) else 0
            record["legacy_signal"] = {
                category: _legacy_candidate_signal(record, old, definition)
                for category, definition in categories.items()
                if set(definition.get("crestix_departments", [])).intersection(record["crestix_departments"])
            }
            record["eligible_categories"] = sorted(record["legacy_signal"])
            records.append(record)
        return records, categories
    finally:
        clinic.close()
        sidecar.close()


class CountingTransport:
    def __init__(self):
        self.transport = PinnedTransport()
        self.request_count = 0

    def get(self, *args, **kwargs):
        self.request_count += 1
        return self.transport.get(*args, **kwargs)


class MeteredFetcher:
    def __init__(self, fetcher: SafeFetcher):
        self.fetcher = fetcher
        self.fetch_attempts = 0
        self.fetch_successes = 0

    def fetch(self, *args, **kwargs):
        self.fetch_attempts += 1
        page = self.fetcher.fetch(*args, **kwargs)
        self.fetch_successes += 1
        return page


def _research_one_clinic(record: dict, categories: list[str], fetcher: SafeFetcher) -> tuple[list[dict], dict]:
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
            # A small, same-host, robots-aware crawl; no search, portal or SNS requests.
            crawled, _errors = crawl(first, fetcher, max_pages=5)
            seen = {p.url for p in pages}
            pages.extend(p for p in crawled if p.url not in seen)
    except WebError as exc:
        fetch_status = "FETCH_ERROR"
        fetch_error = str(exc)

    fetch_attempts = fetcher.fetch_attempts - fetch_attempts_before
    fetch_successes = fetcher.fetch_successes - fetch_successes_before
    checked_at = datetime.now(timezone.utc).isoformat()
    results = []
    official_pages = [p for p in pages if is_official_candidate(p.url)]
    evidence = [{"url": p.url, "page_title": p.title, "blocks": build_evidence_blocks(p),
                 "source_type": "OFFICIAL_HP"} for p in official_pages]
    for category in categories:
        if fetch_status == "IDENTITY_NOT_VERIFIED" or fetch_status == "FETCH_ERROR":
            result = {
                "clinic_id": record["clinic_id"], "treatment_category": category, "status": "REVIEW",
                "evidence_text": "", "evidence_url": "", "evidence_page_title": "",
                "evidence_source_type": "OFFICIAL_HP", "checked_at": checked_at,
                "rule_version": RULE_VERSION, "evidence_engine_version": EVIDENCE_ENGINE_VERSION,
                "matched_alias": "", "reason": fetch_status,
                "page_type": "", "section_heading": "", "provider_context": "UNKNOWN", "exclusion_context": "NONE",
            }
        else:
            result = evaluate_treatment_evidence(category, evidence, clinic_id=record["clinic_id"],
                                                   checked_at=checked_at, clinic_name=record["clinic_name"])
        negative_context = result["evidence_text"] if result.get("reason") == "EXPLICIT_NEGATIVE_OR_REFERRAL" else ""
        results.append({
            "clinic_id": record["clinic_id"], "clinic_name": record["clinic_name"],
            "crestix_department": ";".join(record["crestix_departments"]),
            "treatment_category": category,
            "candidate_bucket": "POSITIVE_CANDIDATE" if record.get("legacy_signal", {}).get(category) else "NEGATIVE_CANDIDATE",
            "research_status": result["status"], "evidence_text": result.get("evidence_text", ""),
            "evidence_url": result.get("evidence_url", ""),
            "evidence_page_title": result.get("evidence_page_title", ""),
            "evidence_source_type": result.get("evidence_source_type", "OFFICIAL_HP"),
            "matched_alias": result.get("matched_alias", ""), "negative_context": negative_context,
            "page_type": result.get("page_type", ""), "section_heading": result.get("section_heading", ""),
            "provider_context": result.get("provider_context", "UNKNOWN"),
            "exclusion_context": result.get("exclusion_context", "NONE"),
            "rule_version": RULE_VERSION, "evidence_engine_version": EVIDENCE_ENGINE_VERSION,
            "researched_at": checked_at,
            "selected_identity_url": record["selected_identity_url"],
            "final_url": pages[0].url if pages else "", "fetch_status": fetch_status,
            "fetch_attempts": fetch_attempts, "fetch_successes": fetch_successes,
            "processing_seconds": round(time.monotonic() - start, 3), "error": fetch_error,
        })
    meta = {"fetch_attempts": fetch_attempts, "fetch_successes": fetch_successes,
            "fetch_status": fetch_status, "processing_seconds": time.monotonic() - start}
    return results, meta


def _write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _read_existing_results(path: Path) -> list[dict]:
    """Recover already-fetched research rows without re-issuing HTTP requests.

    Older runs could carry stray NUL/control bytes from fetched pages, which break
    Python's csv module ("line contains NUL"). Sanitizing the whole decoded text
    before parsing repairs those rows in place; classification fields (research_status,
    matched_alias, ...) are carried through unchanged.
    """
    text = sanitize_text(path.read_text(encoding="utf-8-sig"))
    rows = [{key: sanitize_text(value) for key, value in row.items()}
            for row in csv.DictReader(io.StringIO(text))]
    for row in rows:
        row["clinic_id"] = int(row["clinic_id"])
        for key in ("fetch_attempts", "fetch_successes"):
            if row.get(key, "") != "":
                row[key] = int(row[key])
        if row.get("processing_seconds", "") != "":
            row["processing_seconds"] = float(row["processing_seconds"])
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-only", action="store_true", help="write deterministic sample without HTTP")
    parser.add_argument("--target", type=int, default=TARGET_UNIQUE_CLINICS)
    parser.add_argument("--rebuild-from-existing", action="store_true",
                         help="reuse already-completed HTTP research results (e.g. after sanitizing "
                              "control characters) and rewrite CSV/JSON/DB outputs without issuing new HTTP requests")
    parser.add_argument("--output-suffix", default="",
                         help="suffix inserted before each output file's extension, e.g. _v2_focus, "
                              "so a re-run with a different evidence engine never overwrites the baseline")
    args = parser.parse_args()
    sample_csv = _suffixed(SAMPLE_CSV, args.output_suffix)
    results_csv = _suffixed(RESULTS_CSV, args.output_suffix)
    human_audit_csv = _suffixed(HUMAN_AUDIT_CSV, args.output_suffix)
    summary_json = _suffixed(SUMMARY_JSON, args.output_suffix)
    results_db = _suffixed(RESULTS_DB, args.output_suffix)

    if args.rebuild_from_existing:
        if not results_csv.exists():
            raise FileNotFoundError(f"no existing results to rebuild from: {results_csv}")
        categories = phase7b_research_categories()
        all_results = _read_existing_results(results_csv)
        old_summary = json.loads(summary_json.read_text(encoding="utf-8")) if summary_json.exists() else {}
        target = old_summary.get("target_unique_clinics", args.target)
        sampling_summary = {
            "unique_clinics": old_summary.get("sample_unique_clinics", len({r["clinic_id"] for r in all_results})),
            "category_candidate_quotas": old_summary.get("sample_candidate_quotas", {}),
        }
        fetch_totals = {
            "http_page_fetch_attempts": old_summary.get("http_page_fetch_attempts", 0),
            "http_page_fetch_successes": old_summary.get("http_page_fetch_successes", 0),
            "http_request_count_including_robots_and_redirects":
                old_summary.get("http_request_count_including_robots_and_redirects", 0),
            "processing_seconds": old_summary.get("processing_seconds", 0),
        }
    else:
        records, categories = _load_inputs()
        for row in records:
            row["eligible_categories"] = sorted(row["legacy_signal"])
        sample_rows, sampling_summary = build_sample(records, categories, args.target)
        _write_csv(sample_csv, sample_rows, ["clinic_id", "clinic_name", "crestix_department",
                   "treatment_category", "candidate_bucket", "selected_identity_url", "url_source"])
        if args.sample_only:
            print(json.dumps(sampling_summary, ensure_ascii=False, indent=2))
            return 0
        target = args.target

        by_id = {r["clinic_id"]: r for r in records}
        categories_by_id: dict[int, list[str]] = {}
        for row in sample_rows:
            categories_by_id.setdefault(row["clinic_id"], []).append(row["treatment_category"])
        transport = CountingTransport()
        safe_fetcher = SafeFetcher(transport=transport, timeout=10, max_bytes=2_000_000, interval=0.6)
        fetcher = MeteredFetcher(safe_fetcher)
        all_results = []
        run_meta = []
        for clinic_id in sorted(categories_by_id):
            clinic_result, meta = _research_one_clinic(by_id[clinic_id], sorted(set(categories_by_id[clinic_id])), fetcher)
            all_results.extend(clinic_result)
            run_meta.append({"clinic_id": clinic_id, **meta})
        page_attempts = sum(item["fetch_attempts"] for item in run_meta)
        page_successes = sum(item["fetch_successes"] for item in run_meta)
        fetch_totals = {
            "http_page_fetch_attempts": page_attempts,
            "http_page_fetch_successes": page_successes,
            "http_request_count_including_robots_and_redirects": transport.request_count,
            "processing_seconds": round(sum(item["processing_seconds"] for item in run_meta), 3),
        }

    fields = ["clinic_id", "clinic_name", "crestix_department", "treatment_category", "candidate_bucket",
              "research_status", "evidence_text", "evidence_url", "evidence_page_title",
              "evidence_source_type", "matched_alias", "negative_context", "page_type", "section_heading",
              "provider_context", "exclusion_context", "rule_version", "evidence_engine_version", "researched_at",
              "selected_identity_url", "final_url", "fetch_status", "fetch_attempts", "fetch_successes",
              "processing_seconds", "error"]
    _write_csv(results_csv, all_results, fields)
    human_fields = ["clinic_id", "clinic_name", "crestix_department", "treatment_category", "candidate_bucket",
                    "research_status", "evidence_text", "evidence_url", "matched_alias", "negative_context",
                    "page_type", "section_heading", "provider_context", "exclusion_context",
                    "human_label", "human_comment"]
    human_rows = [{**{key: row.get(key, "") for key in human_fields}, "human_label": "", "human_comment": ""}
                  for row in all_results]
    _write_csv(human_audit_csv, human_rows, human_fields)

    status_counts = {status: sum(row["research_status"] == status for row in all_results)
                     for status in ("CONFIRMED", "REVIEW", "NOT_CONFIRMED")}
    category_counts = {}
    for category in categories:
        subset = [row for row in all_results if row["treatment_category"] == category]
        category_counts[category] = {status: sum(row["research_status"] == status for row in subset)
                                     for status in ("CONFIRMED", "REVIEW", "NOT_CONFIRMED")}
    alias_counts = {}
    for row in all_results:
        if row["matched_alias"]:
            alias_counts[row["matched_alias"]] = alias_counts.get(row["matched_alias"], 0) + 1
    negative_hits = sum(bool(row["negative_context"]) for row in all_results)
    page_attempts = fetch_totals["http_page_fetch_attempts"]
    page_successes = fetch_totals["http_page_fetch_successes"]
    summary = {
        "taxonomy_version": RULE_VERSION, "sample_seed": SAMPLE_SEED,
        "target_unique_clinics": target, "sample_unique_clinics": sampling_summary["unique_clinics"],
        "treatment_clinic_pairs": len(all_results), "sample_candidate_quotas": sampling_summary["category_candidate_quotas"],
        "http_page_fetch_attempts": page_attempts, "http_page_fetch_successes": page_successes,
        "http_page_fetch_success_rate": round(page_successes / page_attempts, 4) if page_attempts else 0,
        "http_request_count_including_robots_and_redirects":
            fetch_totals["http_request_count_including_robots_and_redirects"],
        "status_counts": status_counts, "active_category_status_counts": category_counts,
        "confirmed_alias_hit_counts": alias_counts, "negative_rule_hit_count": negative_hits,
        "mean_treatments_per_clinic": round(len(all_results) / max(sampling_summary["unique_clinics"], 1), 3),
        "processing_seconds": fetch_totals["processing_seconds"],
        "human_audit_labels_blank": all(not row["human_label"] for row in human_rows),
        "phase7c_started": False,
        "evidence_engine_version": EVIDENCE_ENGINE_VERSION,
    }
    summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with sqlite3.connect(results_db) as db:
        db.execute("DROP TABLE IF EXISTS clinic_treatment_research")
        db.execute("""CREATE TABLE clinic_treatment_research (
            clinic_id INTEGER NOT NULL, treatment_category TEXT NOT NULL,
            research_status TEXT NOT NULL CHECK(research_status IN ('CONFIRMED','REVIEW','NOT_CONFIRMED')),
            evidence_text TEXT NOT NULL DEFAULT '', evidence_url TEXT NOT NULL DEFAULT '',
            evidence_page_title TEXT NOT NULL DEFAULT '', evidence_source_type TEXT NOT NULL,
            matched_alias TEXT NOT NULL DEFAULT '', negative_context TEXT NOT NULL DEFAULT '',
            page_type TEXT NOT NULL DEFAULT '', section_heading TEXT NOT NULL DEFAULT '',
            provider_context TEXT NOT NULL DEFAULT '', exclusion_context TEXT NOT NULL DEFAULT '',
            rule_version TEXT NOT NULL, evidence_engine_version TEXT NOT NULL DEFAULT '', researched_at TEXT NOT NULL,
            PRIMARY KEY(clinic_id,treatment_category))""")
        db.executemany("""INSERT INTO clinic_treatment_research VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(r["clinic_id"], r["treatment_category"], r["research_status"], r["evidence_text"],
              r["evidence_url"], r["evidence_page_title"], r["evidence_source_type"], r["matched_alias"],
              r["negative_context"], r.get("page_type", ""), r.get("section_heading", ""),
              r.get("provider_context", ""), r.get("exclusion_context", ""),
              r["rule_version"], r.get("evidence_engine_version", ""), r["researched_at"]) for r in all_results])
        db.commit()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
