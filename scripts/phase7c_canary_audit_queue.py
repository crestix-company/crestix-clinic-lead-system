"""Build the Phase 7-C Canary human audit queue from already-computed research results.

No new judgement happens here and no HTTP is issued. CONFIRMED is audited in full;
REVIEW is capped at 100 (round-robin across categories, evidence-rich rows preferred);
NOT_CONFIRMED is stratified at up to 2 per ACTIVE category (capped at 86), mirroring the
Phase 7-B Pilot audit design so FALSE_NEGATIVE risk is checked the same way.
"""
from __future__ import annotations

import argparse
import csv
from collections import defaultdict
import hashlib
from pathlib import Path

from src.utils.config import ROOT

OUTPUT_DIR = ROOT / "mhlw_dry_run"
RESULTS_CSV = OUTPUT_DIR / "phase7c_canary_500_results.csv"
QUEUE_CSV = OUTPUT_DIR / "phase7c_canary_500_audit_queue.csv"
SEED = "phase7c-canary-500-20260930-audit"
REVIEW_CAP = 100
NOT_CONFIRMED_PER_CATEGORY = 2
NOT_CONFIRMED_CAP = 86

QUEUE_FIELDS = ["clinic_id", "clinic_name", "treatment_category", "research_status", "matched_alias",
                "source_url", "page_title", "page_type", "section_heading", "provider_context",
                "exclusion_context", "evidence_text", "fetch_status", "pages_crawled",
                "evidence_engine_version", "human_label", "human_comment", "audit_required", "audit_reason"]


def _stable_key(*parts: object) -> str:
    return hashlib.sha256("|".join(str(p) for p in (SEED,) + parts).encode()).hexdigest()


def select_review_sample(rows: list[dict], cap: int = REVIEW_CAP) -> list[dict]:
    by_category: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row["research_status"] == "REVIEW":
            by_category[row["treatment_category"]].append(row)

    def priority(row: dict) -> tuple:
        evidence_rank = 0 if row["evidence_text"].strip() else 1
        return (evidence_rank, _stable_key(row["treatment_category"], row["clinic_id"]))

    for category in by_category:
        by_category[category].sort(key=priority)

    categories = sorted(by_category, key=lambda c: _stable_key("cat-order", c))
    selected: list[dict] = []
    idx = 0
    while len(selected) < cap and any(by_category[c] for c in categories):
        c = categories[idx % len(categories)]
        if by_category[c]:
            selected.append(by_category[c].pop(0))
        idx += 1
    return selected


def select_not_confirmed_sample(rows: list[dict], per_category: int = NOT_CONFIRMED_PER_CATEGORY,
                                 cap: int = NOT_CONFIRMED_CAP) -> list[dict]:
    by_category: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row["research_status"] == "NOT_CONFIRMED":
            by_category[row["treatment_category"]].append(row)

    def priority(row: dict) -> tuple:
        evidence_rank = 0 if row["evidence_text"].strip() else 1
        return (evidence_rank, _stable_key(row["treatment_category"], row["clinic_id"]))

    selected = []
    for category in sorted(by_category):
        ranked = sorted(by_category[category], key=priority)
        selected.extend(ranked[:per_category])
    return selected[:cap]


def build_audit_queue(results_rows: list[dict]) -> list[dict]:
    confirmed = [r for r in results_rows if r["research_status"] == "CONFIRMED"]
    review_sample = select_review_sample(results_rows)
    not_confirmed_sample = select_not_confirmed_sample(results_rows)

    queue = []
    for reason, group in (("ALL_CONFIRMED", confirmed), ("REVIEW_STRATIFIED", review_sample),
                          ("NOT_CONFIRMED_STRATIFIED", not_confirmed_sample)):
        for row in group:
            queue.append({
                **{key: row.get(key, "") for key in QUEUE_FIELDS if key not in
                   ("human_label", "human_comment", "audit_required", "audit_reason")},
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
    parser.add_argument("--results-path", type=Path, default=RESULTS_CSV)
    parser.add_argument("--queue-path", type=Path, default=QUEUE_CSV)
    args = parser.parse_args()
    results_rows = _read_results(args.results_path)
    queue = build_audit_queue(results_rows)
    _write_csv(args.queue_path, queue, QUEUE_FIELDS)
    from collections import Counter
    reasons = Counter(row["audit_reason"] for row in queue)
    print(f"audit_queue_rows={len(queue)} breakdown={dict(reasons)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
