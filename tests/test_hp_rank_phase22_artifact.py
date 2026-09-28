import json

import pytest

from src.scoring.hp_rank_phase22 import fit_phase22
from src.scoring.hp_rank_phase22_artifact import (
    ArtifactValidationError,
    FEATURE_SCHEMA_VERSION,
    artifact_bytes,
    build_artifact,
    dataset_fingerprint,
    load_artifact,
)


def _rows():
    rows = []
    labels = ("A", "A", "B", "B", "C", "C", "D", "D")
    for index, label in enumerate(labels, start=1):
        high = label in ("A", "B")
        rows.append({
            "no": index,
            "clinic_id": 1000 + index,
            "hp_url": f"https://example{index}.test/",
            "human_rank": label,
            "p1_rank": label,
            "p1_score": 7 if high else 2,
            "candidate_score": 7 if high else 2,
            "features": {"HTTPS": True, "Web予約導線": label == "A"},
            "new_features": {"page_count": 8 if high else 2},
            "html_features": {
                "html_byte_size": 150000 if label != "D" else 30000,
                "visible_text_length": 4000 if high else 1000,
                "image_count": 20 if label != "D" else 2,
                "internal_link_count": 30 if high else 5,
                "iframe_count": 0,
                "table_count": 1,
                "inline_style_ratio": 0.01,
                "viewport_meta": True,
                "phone_cta": label != "D",
                "doctor_photo_candidate": label in ("A", "C"),
                "clinic_photo_many": label != "D",
            },
        })
    return rows


def _write(tmp_path, artifact, name="artifact.json"):
    path = tmp_path / name
    path.write_bytes(artifact_bytes(artifact))
    return path


def test_artifact_is_deterministic_and_fingerprint_is_order_independent():
    rows = _rows()
    first = build_artifact(rows)
    second = build_artifact(list(reversed(rows)))
    assert artifact_bytes(first) == artifact_bytes(second)
    assert dataset_fingerprint(rows, ("HTTPS", "Web予約導線")) == dataset_fingerprint(
        list(reversed(rows)), ("HTTPS", "Web予約導線")
    )


def test_loader_round_trip_reproduces_predictions(tmp_path):
    rows = _rows()
    direct = fit_phase22(rows)
    loaded = load_artifact(_write(tmp_path, build_artifact(rows)))
    assert [loaded.predict(row) for row in rows] == [direct.predict(row) for row in rows]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("model_version", "wrong", "model version mismatch"),
        ("feature_schema_version", "wrong", "feature schema mismatch"),
    ],
)
def test_loader_rejects_version_mismatch(tmp_path, field, value, message):
    artifact = build_artifact(_rows())
    artifact[field] = value
    with pytest.raises(ArtifactValidationError, match=message):
        load_artifact(_write(tmp_path, artifact))


def test_loader_rejects_missing_field(tmp_path):
    artifact = build_artifact(_rows())
    del artifact["models"]
    with pytest.raises(ArtifactValidationError, match="missing required fields"):
        load_artifact(_write(tmp_path, artifact))


def test_loader_rejects_feature_order_mismatch(tmp_path):
    artifact = build_artifact(_rows())
    artifact["feature_order"][0] = "wrong"
    with pytest.raises(ArtifactValidationError, match="feature order mismatch"):
        load_artifact(_write(tmp_path, artifact))


def test_committed_artifact_has_expected_schema():
    artifact = json.loads(
        open("artifacts/hp_rank/hp-content-2.2-hierarchical-logistic-experimental.json", encoding="utf-8").read()
    )
    assert artifact["feature_schema_version"] == FEATURE_SCHEMA_VERSION
    assert artifact["training_row_count"] == 300
    assert artifact["label_distribution"] == {"A": 7, "B": 83, "C": 170, "D": 40}
