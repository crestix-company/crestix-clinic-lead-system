"""Serialization for the frozen Phase2.2 experimental model artifact.

This module is intentionally not imported by the production scoring path.
It packages and validates the already-frozen candidate without changing its
features, thresholds, optimizer, or model structure.
"""

import hashlib
import json
from collections import Counter
from pathlib import Path

from src.scoring.hp_rank_phase22 import (
    AB_THRESHOLD,
    A_THRESHOLD,
    CACHE_BOOL_FEATURES,
    CACHE_NUM_FEATURES,
    CANDIDATE_STATUS,
    D_ADD_THRESHOLD,
    HTML_BOOL_FEATURES,
    HTML_NUM_FEATURES,
    MODEL_VERSION,
    BinaryLogisticModel,
    Phase22Model,
    feature_vector,
    fit_phase22,
)


ARTIFACT_FORMAT_VERSION = 1
FEATURE_SCHEMA_VERSION = "hp-rank-phase22-feature-vector-v1"
CODE_VERSION = "hp-rank-phase22-artifact-v1"
FROZEN_SOURCE_COMMIT = "606d7f48085186c5e850da28365fa5a245b29576"
RANDOM_SEED = 0  # Recorded for reproducibility; the optimizer itself is deterministic.


class ArtifactValidationError(ValueError):
    """Raised when a Phase2.2 artifact is incomplete or incompatible."""


def canonical_json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def feature_order(base_feature_names):
    return (
        [f"base.bool:{name}" for name in base_feature_names]
        + [f"html.bool:{name}" for name in HTML_BOOL_FEATURES]
        + [f"html.log1p_nonnegative:{name}" for name in HTML_NUM_FEATURES]
        + [f"cache.bool:{name}" for name in CACHE_BOOL_FEATURES]
        + [f"cache.log1p_nonnegative:{name}" for name in CACHE_NUM_FEATURES]
        + ["row.float:p1_score", "row.float:candidate_score", "row.bool:html_features_missing"]
    )


def _identity(row):
    identity = {
        "clinic_id": row.get("clinic_id"),
        "no": row.get("no"),
        "hp_url": row.get("hp_url") or "",
    }
    if identity["clinic_id"] is None or identity["no"] is None:
        raise ValueError("dataset row is missing clinic_id or no identity")
    return identity


def dataset_fingerprint(rows, base_feature_names):
    """Hash identity, human label, and exact model input independent of row order."""
    records = [
        {
            "identity": _identity(row),
            "human_label": row["human_rank"],
            "model_input": feature_vector(row, base_feature_names),
        }
        for row in rows
    ]
    records.sort(key=lambda record: canonical_json_bytes(record["identity"]))
    return hashlib.sha256(canonical_json_bytes(records)).hexdigest()


def _serialize_binary(model):
    return {
        "intercept": model.weights[0],
        "coefficients": list(model.weights[1:]),
        "means": list(model.means),
        "scales": list(model.scales),
    }


def _deserialize_binary(value, width, name):
    required = {"intercept", "coefficients", "means", "scales"}
    if not isinstance(value, dict) or not required.issubset(value):
        raise ArtifactValidationError(f"{name} is missing required fields")
    coefficients = value["coefficients"]
    means = value["means"]
    scales = value["scales"]
    if not all(isinstance(items, list) and len(items) == width for items in (coefficients, means, scales)):
        raise ArtifactValidationError(f"{name} feature width mismatch")
    if any(scale <= 0 for scale in scales):
        raise ArtifactValidationError(f"{name} contains a non-positive scale")
    return BinaryLogisticModel(
        weights=(float(value["intercept"]), *(float(item) for item in coefficients)),
        means=tuple(float(item) for item in means),
        scales=tuple(float(item) for item in scales),
    )


def build_artifact(rows, source_commit=FROZEN_SOURCE_COMMIT):
    """Fit the frozen design on all labeled rows and return a JSON-safe artifact."""
    ordered_rows = sorted(rows, key=lambda row: canonical_json_bytes(_identity(row)))
    model = fit_phase22(ordered_rows)
    order = feature_order(model.base_feature_names)
    labels = Counter(row["human_rank"] for row in ordered_rows)
    return {
        "artifact_format_version": ARTIFACT_FORMAT_VERSION,
        "model_version": MODEL_VERSION,
        "candidate_status": CANDIDATE_STATUS,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feature_order": order,
        "preprocessing_rules": {
            "boolean": "bool(value) converted to 0.0 or 1.0; missing is false",
            "numeric": "max(0.0, float(value or 0)) then log1p for declared numeric features",
            "scores": "float(value or 0), without log transform",
            "standardization": "training population mean and population standard deviation; scale 1.0 if sd < 1e-8",
            "missing_html": "all HTML features default to zero plus explicit missing indicator",
        },
        "model_structure": {
            "type": "hierarchical-class-balanced-logistic-regression",
            "stages": ["AB_vs_CD", "A_vs_B", "C_vs_D"],
            "phase21_d_decisions_preserved": True,
            "optimizer": {"l2": 1.0, "steps": 2000, "learning_rate": 0.05},
        },
        "thresholds": {
            "ab": AB_THRESHOLD,
            "a_within_ab": A_THRESHOLD,
            "d_add_within_cd": D_ADD_THRESHOLD,
        },
        "random_seed": RANDOM_SEED,
        "training_row_count": len(ordered_rows),
        "label_distribution": {rank: labels.get(rank, 0) for rank in ("A", "B", "C", "D")},
        "training_dataset_fingerprint": dataset_fingerprint(ordered_rows, model.base_feature_names),
        "source_commit": source_commit,
        "code_version": CODE_VERSION,
        "base_feature_names": list(model.base_feature_names),
        "models": {
            "ab": _serialize_binary(model.ab_model),
            "a": _serialize_binary(model.a_model),
            "d": _serialize_binary(model.d_model),
        },
    }


def artifact_bytes(artifact):
    """Stable, human-diffable JSON representation with a trailing newline."""
    return (json.dumps(artifact, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def artifact_sha256(artifact):
    return hashlib.sha256(artifact_bytes(artifact)).hexdigest()


def load_artifact(path):
    """Load a frozen artifact and fail closed on missing or incompatible data."""
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactValidationError(f"cannot read artifact: {exc}") from exc
    required = {
        "artifact_format_version", "model_version", "candidate_status",
        "feature_schema_version", "feature_order", "preprocessing_rules",
        "model_structure", "thresholds", "random_seed", "training_row_count",
        "label_distribution", "training_dataset_fingerprint", "source_commit",
        "code_version", "base_feature_names", "models",
    }
    if not isinstance(value, dict) or not required.issubset(value):
        raise ArtifactValidationError("artifact is missing required fields")
    if value["artifact_format_version"] != ARTIFACT_FORMAT_VERSION:
        raise ArtifactValidationError("artifact format version mismatch")
    if value["model_version"] != MODEL_VERSION:
        raise ArtifactValidationError("model version mismatch")
    if value["candidate_status"] != CANDIDATE_STATUS:
        raise ArtifactValidationError("candidate status mismatch")
    if value["feature_schema_version"] != FEATURE_SCHEMA_VERSION:
        raise ArtifactValidationError("feature schema mismatch")
    base_names = value["base_feature_names"]
    if not isinstance(base_names, list) or value["feature_order"] != feature_order(base_names):
        raise ArtifactValidationError("feature order mismatch")
    expected_thresholds = {"ab": AB_THRESHOLD, "a_within_ab": A_THRESHOLD, "d_add_within_cd": D_ADD_THRESHOLD}
    if value["thresholds"] != expected_thresholds:
        raise ArtifactValidationError("frozen threshold mismatch")
    if value["source_commit"] != FROZEN_SOURCE_COMMIT or value["code_version"] != CODE_VERSION:
        raise ArtifactValidationError("source or code version mismatch")
    models = value["models"]
    if not isinstance(models, dict) or set(models) != {"ab", "a", "d"}:
        raise ArtifactValidationError("model structure mismatch")
    width = len(value["feature_order"])
    return Phase22Model(
        base_feature_names=tuple(base_names),
        ab_model=_deserialize_binary(models["ab"], width, "ab model"),
        a_model=_deserialize_binary(models["a"], width, "a model"),
        d_model=_deserialize_binary(models["d"], width, "d model"),
    )
