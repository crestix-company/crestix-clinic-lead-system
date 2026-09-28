#!/usr/bin/env python3
"""Run a deterministic bounded Shadow comparison from existing data only."""

import argparse
import csv
import json
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from src.scoring.hp_rank_phase22_artifact import load_artifact
from src.scoring.hp_rank_phase22_bounded_shadow import (
    aggregate_agreement,
    build_review_queue,
    deterministic_sample,
)
from src.scoring.hp_rank_phase22_shadow import ShadowValidationError, load_model, predict_with_metadata


PREDICTION_FIELDS = (
    "clinic_id", "medical_key", "clinic_name", "current_rank", "shadow_rank",
    "shadow_score", "model_version", "artifact_sha", "prediction_status",
)
REVIEW_FIELDS = PREDICTION_FIELDS + ("review_priority", "review_reason")


def _write_csv(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows({field: row.get(field, "") for field in fields} for row in rows)


def _model_row(source):
    # Explicit allowlist: human_rank and split are never passed to the model.
    return {
        "p1_rank": source.get("p1_rank"),
        "p1_score": source.get("p1_score"),
        "candidate_score": source.get("candidate_score"),
        "features": source.get("features"),
        "html_features": source.get("html_features"),
        "new_features": source.get("new_features"),
    }


def run(dataset_path, db_path, artifact_path, limit=500):
    source_rows = json.loads(dataset_path.read_text(encoding="utf-8"))
    ids = sorted({int(row["clinic_id"]) for row in source_rows})
    placeholders = ",".join("?" for _ in ids)
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    records = connection.execute(
        f"SELECT id, medical_key, clinic_name, hp_rank FROM clinics WHERE id IN ({placeholders})",
        ids,
    ).fetchall()
    connection.close()
    current_by_id = {row["id"]: dict(row) for row in records}
    eligible = []
    seen = set()
    for source in source_rows:
        clinic_id = int(source["clinic_id"])
        current = current_by_id.get(clinic_id)
        if clinic_id in seen or not current or current["hp_rank"] not in ("A", "B", "C", "D"):
            continue
        if not current["medical_key"]:
            continue
        seen.add(clinic_id)
        eligible.append({"source": source, **current})
    sample = deterministic_sample(eligible, limit=limit)
    shadow_model = load_model(artifact_path)
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    # Loading twice is intentional: strict validation plus explicit metadata
    # reporting from the immutable artifact file.
    load_artifact(artifact_path)
    predictions = []
    for item in sample:
        base = {
            "clinic_id": item["id"],
            "medical_key": item["medical_key"],
            "clinic_name": item["clinic_name"],
            "current_rank": item["hp_rank"],
            "shadow_rank": "",
            "shadow_score": "",
            "model_version": artifact["model_version"],
            "artifact_sha": shadow_model.artifact_sha,
            "prediction_status": "REVIEW:FEATURE_MISSING",
        }
        try:
            metadata = predict_with_metadata(shadow_model, _model_row(item["source"]))
            base.update({
                "shadow_rank": metadata["phase22_rank"],
                "shadow_score": metadata["phase22_score"],
                "prediction_status": "OK",
            })
        except (ShadowValidationError, KeyError, TypeError, ValueError, OverflowError):
            pass
        except Exception:
            base["prediction_status"] = "REVIEW:PREDICTION_EXCEPTION"
        predictions.append(base)
    summary = aggregate_agreement(predictions)
    summary["sample_limit"] = limit
    summary["source_rows"] = len(source_rows)
    summary["artifact"] = {
        "model_version": artifact["model_version"],
        "artifact_sha": shadow_model.artifact_sha,
        "dataset_fingerprint": artifact["training_dataset_fingerprint"],
    }
    return predictions, build_review_queue(predictions), summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--review-queue", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    args = parser.parse_args()
    predictions, queue, summary = run(args.dataset, args.db, args.artifact, args.limit)
    _write_csv(args.predictions, predictions, PREDICTION_FIELDS)
    _write_csv(args.review_queue, queue, REVIEW_FIELDS)
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
    print(f"review_queue={len(queue)}")


if __name__ == "__main__":
    main()
