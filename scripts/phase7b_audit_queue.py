"""Build the Phase 7-B human audit queue from already-computed research results.

No new judgement happens here and no HTTP is issued: this only selects which
already-scored rows a human should review, and writes human_label/human_comment
blank for them to fill in. CONFIRMED and REVIEW are audited in full; NOT_CONFIRMED
is stratified per ACTIVE treatment category since auditing all 543 rows is not
required for this pilot.
"""
from __future__ import annotations

import argparse
import csv
from collections import defaultdict
import hashlib
from pathlib import Path

from src.utils.config import ROOT

OUTPUT_DIR = ROOT / "mhlw_dry_run"
RESULTS_CSV = OUTPUT_DIR / "phase7b_results.csv"
QUEUE_CSV = OUTPUT_DIR / "phase7b_human_audit_queue.csv"
SAMPLE_SEED = "7A-v2-phase7b-pilot-20260929"
NOT_CONFIRMED_PER_CATEGORY = 2

QUEUE_FIELDS = ["clinic_id", "clinic_name", "crestix_department", "treatment_category", "candidate_bucket",
                "research_status", "evidence_text", "evidence_url", "matched_alias", "negative_context",
                "page_type", "section_heading", "provider_context", "exclusion_context",
                "human_label", "human_comment", "audit_required", "audit_reason"]


def _stable_key(*parts: object) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()


def select_not_confirmed_sample(rows: list[dict], per_category: int = NOT_CONFIRMED_PER_CATEGORY) -> list[dict]:
    """Deterministic stratified NOT_CONFIRMED sample, >=`per_category` per ACTIVE category.

    Within each category, rows with a POSITIVE_CANDIDATE legacy signal or with any
    captured evidence_text are preferred first (they are the most informative for
    spotting false negatives); ties break on a stable seeded hash so the sample is
    reproducible without random.seed().
    """
    by_category: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row["research_status"] == "NOT_CONFIRMED":
            by_category[row["treatment_category"]].append(row)

    def priority(row: dict) -> tuple:
        bucket_rank = 0 if row["candidate_bucket"] == "POSITIVE_CANDIDATE" else 1
        evidence_rank = 0 if row["evidence_text"].strip() else 1
        return (bucket_rank, evidence_rank, _stable_key(SAMPLE_SEED, row["treatment_category"], row["clinic_id"]))

    selected = []
    for category in sorted(by_category):
        ranked = sorted(by_category[category], key=priority)
        selected.extend(ranked[:per_category])
    return selected


def build_audit_queue(results_rows: list[dict]) -> list[dict]:
    confirmed = [r for r in results_rows if r["research_status"] == "CONFIRMED"]
    review = [r for r in results_rows if r["research_status"] == "REVIEW"]
    not_confirmed_sample = select_not_confirmed_sample(results_rows)

    queue = []
    for reason, group in (("ALL_CONFIRMED", confirmed), ("ALL_REVIEW", review),
                          ("NOT_CONFIRMED_STRATIFIED", not_confirmed_sample)):
        for row in group:
            queue.append({
                **{key: row.get(key, "") for key in QUEUE_FIELDS if key not in ("human_label", "human_comment",
                                                                                  "audit_required", "audit_reason")},
                "human_label": "", "human_comment": "",
                "audit_required": "TRUE", "audit_reason": reason,
            })
    return queue


def _read_results(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-path", type=Path, default=RESULTS_CSV,
                         help="phase7b results CSV to build the audit queue from")
    parser.add_argument("--queue-path", type=Path, default=QUEUE_CSV,
                         help="output path for the human audit queue CSV")
    args = parser.parse_args()
    results_rows = _read_results(args.results_path)
    queue = build_audit_queue(results_rows)
    _write_csv(args.queue_path, queue, QUEUE_FIELDS)
    reasons = defaultdict(int)
    for row in queue:
        reasons[row["audit_reason"]] += 1
    print(f"audit_queue_rows={len(queue)} breakdown={dict(reasons)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
