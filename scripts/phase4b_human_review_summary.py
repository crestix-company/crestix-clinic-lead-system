"""Read-only summary of human-entered audit_label values for the Phase-4B
Production adoption review (confirmed_human_review_100.csv, review_all_22.csv).

Does not touch Production DB, Treatment sidecar, MHLW DB, the Phase-4B cache,
or re-crawl anything. Reads two local CSV files and prints/writes a summary.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "hybrid_phase4b"
CONFIRMED_SAMPLE = OUT / "confirmed_human_review_100_labeled.csv"
REVIEW_ALL = OUT / "review_all_22_labeled.csv"

ALLOWED_LABELS = {"", "CORRECT", "INCORRECT", "UNCERTAIN"}


def read_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def validate_labels(rows: list[dict], source: str) -> None:
    bad = sorted({r["audit_label"] for r in rows if r.get("audit_label", "") not in ALLOWED_LABELS})
    if bad:
        raise ValueError(f"{source}: invalid audit_label value(s) {bad!r}; allowed: {sorted(ALLOWED_LABELS)}")


def label_counts(rows: list[dict]) -> dict:
    counts = Counter(r.get("audit_label", "") for r in rows)
    return {
        "CORRECT": counts.get("CORRECT", 0),
        "INCORRECT": counts.get("INCORRECT", 0),
        "UNCERTAIN": counts.get("UNCERTAIN", 0),
        "UNLABELED": counts.get("", 0),
        "total": len(rows),
    }


def accuracy(counts: dict) -> float | None:
    denom = counts["CORRECT"] + counts["INCORRECT"]
    return round(counts["CORRECT"] / denom, 4) if denom else None


def grouped_summary(rows: list[dict], key: str) -> dict:
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r[key], []).append(r)
    result = {}
    for name, grp in sorted(groups.items()):
        counts = label_counts(grp)
        result[name] = {**counts, "accuracy": accuracy(counts)}
    return result


def summarize_confirmed(rows: list[dict]) -> dict:
    overall = label_counts(rows)
    return {
        "overall": {**overall, "accuracy": accuracy(overall)},
        "by_signal_source": grouped_summary(rows, "signal_source"),
        "by_treatment_category": grouped_summary(rows, "treatment_category"),
    }


def summarize_review(rows: list[dict]) -> dict:
    overall = label_counts(rows)
    return {"overall": {**overall, "accuracy": accuracy(overall)}}


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--confirmed", type=Path, default=CONFIRMED_SAMPLE,
                     help="path to the CONFIRMED sample CSV with a human-filled audit_label column")
    ap.add_argument("--review", type=Path, default=REVIEW_ALL,
                     help="path to the REVIEW-all CSV with a human-filled audit_label column")
    return ap.parse_args()


def main() -> dict:
    args = parse_args()
    confirmed_rows = read_rows(args.confirmed)
    review_rows = read_rows(args.review)
    validate_labels(confirmed_rows, args.confirmed.name)
    validate_labels(review_rows, args.review.name)

    report = {
        "confirmed_sample": summarize_confirmed(confirmed_rows),
        "review_all": summarize_review(review_rows),
    }
    (OUT / "human_review_summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    main()
