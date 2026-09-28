#!/usr/bin/env python3
"""Run label-free Phase2.2 shadow scoring without touching Production."""

import argparse
import csv
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from src.scoring.hp_rank_phase22 import MODEL_VERSION
from src.scoring.hp_rank_phase22_shadow import (
    ShadowValidationError,
    aggregate_transitions,
    build_features,
    load_model,
    load_phase21_config,
    predict_with_metadata,
)


FIELDS = (
    "review_id", "phase21_rank", "phase21_score", "phase22_rank", "phase22_score",
    "model_version", "artifact_sha", "prediction_status",
)


def _read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def run(blind_path, reference_path, candidates_path, html_dir, artifact_path, config_path):
    blind = _read_csv(blind_path)
    reference = _read_csv(reference_path)
    candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
    if len(blind) != 300 or len(reference) != 300 or len(candidates) != 300:
        raise ShadowValidationError("shadow inputs must each contain exactly 300 rows")
    if any((row.get("human_rank") or "").strip() for row in blind):
        raise ShadowValidationError("blind input unexpectedly contains human labels")
    blind_ids = [row["review_id"] for row in blind]
    reference_by_id = {row["review_id"]: row for row in reference}
    candidate_by_id = {str(row["id"]): row for row in candidates}
    if len(set(blind_ids)) != 300 or set(blind_ids) != set(reference_by_id):
        raise ShadowValidationError("blind/reference review_id mismatch")
    shadow_model = load_model(artifact_path)
    config = load_phase21_config(config_path)
    output = []
    for review_id in blind_ids:
        reference_row = reference_by_id[review_id]
        candidate = candidate_by_id.get(reference_row["id"])
        base = {
            "review_id": review_id,
            "phase21_rank": "",
            "phase21_score": "",
            "phase22_rank": "",
            "phase22_score": "",
            "model_version": MODEL_VERSION,
            "artifact_sha": shadow_model.artifact_sha,
            "prediction_status": "REVIEW",
        }
        if candidate is None:
            base["prediction_status"] = "REVIEW:MISSING_CANDIDATE"
            output.append(base)
            continue
        html_path = html_dir / f"{candidate['id']}.html"
        try:
            html = html_path.read_text(encoding="utf-8", errors="replace")
            row = build_features(candidate, html, config)
            metadata = predict_with_metadata(shadow_model, row)
            metadata["phase22_score"] = f"{metadata['phase22_score']:.12f}"
            output.append({"review_id": review_id, **metadata})
        except FileNotFoundError:
            base["prediction_status"] = "REVIEW:MISSING_HTML"
            output.append(base)
        except (OSError, ShadowValidationError, KeyError, TypeError, ValueError, OverflowError) as exc:
            base["prediction_status"] = f"REVIEW:{type(exc).__name__}"
            output.append(base)
        except Exception:
            # A row-level prediction exception must never invent a fallback
            # rank. Artifact/config loading happens before this loop and still
            # aborts the complete run fail-closed.
            base["prediction_status"] = "REVIEW:PREDICTION_EXCEPTION"
            output.append(base)
    return output, aggregate_transitions(output)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--blind", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--html-dir", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=REPO_ROOT / "config/hp_ranking_candidate.yml")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    args = parser.parse_args()
    rows, summary = run(
        args.blind, args.reference, args.candidates, args.html_dir, args.artifact, args.config,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
