#!/usr/bin/env python3
"""Build the frozen Phase2.2 artifact from the existing 300-row GT dataset."""

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from src.scoring.hp_rank_phase22_artifact import artifact_bytes, artifact_sha256, build_artifact


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    rows = json.loads(args.dataset.read_text(encoding="utf-8"))
    artifact = build_artifact(rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(artifact_bytes(artifact))
    print(f"artifact={args.output}")
    print(f"rows={artifact['training_row_count']}")
    print(f"dataset_fingerprint={artifact['training_dataset_fingerprint']}")
    print(f"artifact_sha256={artifact_sha256(artifact)}")


if __name__ == "__main__":
    main()
