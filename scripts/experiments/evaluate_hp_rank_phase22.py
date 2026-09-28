#!/usr/bin/env python3
"""Reproduce the frozen Phase2.2 experiment; this is not a production runner."""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from src.scoring.hp_rank_phase22 import (
    CANDIDATE_STATUS, MODEL_VERSION, RANKS, fit_phase22, phase21_baseline_rank,
)


ORDER = {rank: index for index, rank in enumerate(RANKS)}


def fixed_folds(rows, count=5):
    """Stable, stratified folds. Labels are used only to balance evaluation folds."""
    result = {}
    for rank in RANKS:
        ranked = sorted((row for row in rows if row["human_rank"] == rank), key=lambda row: row["no"])
        for index, row in enumerate(ranked):
            result[row["no"]] = index % count
    return result


def evaluate(rows, predictions):
    matrix = {actual: {predicted: 0 for predicted in RANKS} for actual in RANKS}
    for row, predicted in zip(rows, predictions):
        matrix[row["human_rank"]][predicted] += 1
    per_rank = {}
    f1_values = []
    for rank in RANKS:
        tp = matrix[rank][rank]
        predicted_count = sum(matrix[actual][rank] for actual in RANKS)
        actual_count = sum(matrix[rank].values())
        precision = tp / predicted_count if predicted_count else 0.0
        recall = tp / actual_count if actual_count else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        per_rank[rank] = {"precision": precision, "recall": recall, "f1": f1, "support": actual_count}
        f1_values.append(f1)
    ab_cd_correct = sum(
        count for actual in RANKS for predicted, count in matrix[actual].items()
        if (actual in ("A", "B")) == (predicted in ("A", "B"))
    )
    severe = sum(
        count for actual in RANKS for predicted, count in matrix[actual].items()
        if abs(ORDER[actual] - ORDER[predicted]) >= 2
    )
    return {
        "confusion_matrix": matrix,
        "per_rank": per_rank,
        "macro_f1": sum(f1_values) / len(f1_values),
        "ab_cd_accuracy": ab_cd_correct / len(rows),
        "severe_errors": severe,
        "prediction_counts": dict(Counter(predictions)),
    }


def run(rows):
    folds = fixed_folds(rows)
    candidate_predictions = [None] * len(rows)
    for fold in range(5):
        training = [row for row in rows if folds[row["no"]] != fold]
        model = fit_phase22(training)
        for index, row in enumerate(rows):
            if folds[row["no"]] == fold:
                candidate_predictions[index] = model.predict(row)
    baseline_predictions = [phase21_baseline_rank(row) for row in rows]
    return {
        "model_version": MODEL_VERSION,
        "candidate_status": CANDIDATE_STATUS,
        "production_connected": False,
        "evaluation": "fixed-stratified-5-fold-out-of-fold",
        "rows": len(rows),
        "class_counts": dict(Counter(row["human_rank"] for row in rows)),
        "phase21_baseline": evaluate(rows, baseline_predictions),
        "phase22_candidate": evaluate(rows, candidate_predictions),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    rows = json.loads(args.dataset.read_text())
    result = run(rows)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.write_text(rendered + "\n")


if __name__ == "__main__":
    main()
