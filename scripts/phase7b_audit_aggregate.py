"""Aggregate Phase 7-B human audit labels into precision/false-positive/false-negative stats.

Reads mhlw_dry_run/phase7b_human_audit_queue.csv after a human has filled in
human_label (TRUE_POSITIVE / FALSE_POSITIVE / TRUE_NEGATIVE / FALSE_NEGATIVE /
UNCERTAIN). Refuses to compute precision while every human_label is still blank,
since an unaudited queue has no ground truth yet.
"""
from __future__ import annotations

import csv
from collections import Counter, defaultdict
import json
from pathlib import Path

from src.utils.config import ROOT

OUTPUT_DIR = ROOT / "mhlw_dry_run"
QUEUE_CSV = OUTPUT_DIR / "phase7b_human_audit_queue.csv"
AGGREGATE_JSON = OUTPUT_DIR / "phase7b_audit_aggregate.json"
VALID_LABELS = frozenset({"TRUE_POSITIVE", "FALSE_POSITIVE", "TRUE_NEGATIVE", "FALSE_NEGATIVE", "UNCERTAIN"})


def _read_queue(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def aggregate(rows: list[dict]) -> dict:
    labeled = [row for row in rows if row.get("human_label", "").strip()]
    if not labeled:
        return {"labeled_count": 0, "message": "human_label is blank for every row; precision not computed."}

    invalid = sorted({row["human_label"] for row in labeled} - VALID_LABELS)
    if invalid:
        raise ValueError(f"unknown human_label value(s): {invalid}")

    confirmed = [row for row in labeled if row["research_status"] == "CONFIRMED"]
    review = [row for row in labeled if row["research_status"] == "REVIEW"]
    not_confirmed = [row for row in labeled if row["research_status"] == "NOT_CONFIRMED"]

    confirmed_tp = sum(row["human_label"] == "TRUE_POSITIVE" for row in confirmed)
    confirmed_fp = sum(row["human_label"] == "FALSE_POSITIVE" for row in confirmed)
    confirmed_uncertain = sum(row["human_label"] == "UNCERTAIN" for row in confirmed)

    review_tp = sum(row["human_label"] == "TRUE_POSITIVE" for row in review)
    review_labeled_decided = sum(row["human_label"] != "UNCERTAIN" for row in review)

    nc_fn = sum(row["human_label"] == "FALSE_NEGATIVE" for row in not_confirmed)
    nc_labeled_decided = sum(row["human_label"] != "UNCERTAIN" for row in not_confirmed)

    negative_phrase_markers = ("行っていません", "対応しておりません", "実施していません", "取り扱っていません",
                                "他院", "紹介先", "連携医療機関", "別の医院", "別の病院")

    severe_fp_negative_sentence = sum(
        1 for row in confirmed
        if row["human_label"] == "FALSE_POSITIVE"
        and any(marker in row.get("evidence_text", "") for marker in negative_phrase_markers)
    )

    by_category: dict[str, dict] = {}
    for row in confirmed:
        cat = row["treatment_category"]
        by_category.setdefault(cat, {"tp": 0, "fp": 0, "uncertain": 0})
        if row["human_label"] == "TRUE_POSITIVE":
            by_category[cat]["tp"] += 1
        elif row["human_label"] == "FALSE_POSITIVE":
            by_category[cat]["fp"] += 1
        elif row["human_label"] == "UNCERTAIN":
            by_category[cat]["uncertain"] += 1
    category_precision = {
        cat: {"true_positive": v["tp"], "false_positive": v["fp"], "uncertain": v["uncertain"],
              "precision": _rate(v["tp"], v["tp"] + v["fp"])}
        for cat, v in sorted(by_category.items())
    }

    by_category_fn: dict[str, int] = defaultdict(int)
    for row in review + not_confirmed:
        if row["human_label"] == "FALSE_NEGATIVE":
            by_category_fn[row["treatment_category"]] += 1

    alias_fp = Counter(row["matched_alias"] for row in confirmed if row["human_label"] == "FALSE_POSITIVE")
    alias_fn = Counter(row["matched_alias"] for row in (review + not_confirmed) if row["human_label"] == "FALSE_NEGATIVE")

    return {
        "labeled_count": len(labeled),
        "unlabeled_count": len(rows) - len(labeled),
        "confirmed_precision": {
            "true_positive": confirmed_tp, "false_positive": confirmed_fp, "uncertain": confirmed_uncertain,
            "labeled_total": len(confirmed), "precision": _rate(confirmed_tp, confirmed_tp + confirmed_fp),
        },
        "review_true_positive_rate": {
            "true_positive": review_tp, "labeled_decided_total": review_labeled_decided,
            "labeled_total": len(review), "rate": _rate(review_tp, review_labeled_decided),
        },
        "not_confirmed_sample_false_negative_rate": {
            "false_negative": nc_fn, "labeled_decided_total": nc_labeled_decided,
            "labeled_total": len(not_confirmed), "rate": _rate(nc_fn, nc_labeled_decided),
        },
        "severe_false_positive_negative_sentence_count": severe_fp_negative_sentence,
        "category_confirmed_precision": category_precision,
        "category_false_negative_candidate_counts": dict(sorted(by_category_fn.items())),
        "alias_false_positive_counts": dict(alias_fp),
        "alias_false_negative_candidate_counts": dict(alias_fn),
    }


def main() -> int:
    rows = _read_queue(QUEUE_CSV)
    summary = aggregate(rows)
    AGGREGATE_JSON.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
