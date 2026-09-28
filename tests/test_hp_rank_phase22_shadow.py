import json
import math

import pytest

from src.scoring.hp_rank_phase22_artifact import artifact_bytes, build_artifact
from src.scoring.hp_rank_phase22_shadow import (
    ShadowValidationError,
    aggregate_transitions,
    build_features,
    load_model,
    predict,
    predict_with_metadata,
)


def _rows():
    rows = []
    for index, label in enumerate(("A", "A", "B", "B", "C", "C", "D", "D"), start=1):
        high = label in ("A", "B")
        rows.append({
            "no": index, "clinic_id": 100 + index, "hp_url": f"https://e{index}.test/",
            "human_rank": label, "p1_rank": label, "p1_score": 6 if high else 1,
            "candidate_score": 6 if high else 1,
            "features": {"HTTPS": True, "Web予約導線": label == "A"},
            "new_features": {
                "thin_single_page": False, "has_copyright_notice": True,
                "copyright_year_recent": True, "copyright_year_old": False,
                "video_signal": False, "total_text_len": 1000,
                "home_text_len": 1000, "page_count": 1, "heading_count_proxy": 2,
            },
            "html_features": {
                "viewport_meta": True, "responsive_indicator": True,
                "table_layout_indicator": False, "video_present": False,
                "sns_link_present": False, "web_reservation_cta": False,
                "contact_cta": True, "phone_cta": label != "D",
                "doctor_photo_candidate": label in ("A", "C"),
                "clinic_photo_many": label != "D", "copyright_year_recent": True,
                "copyright_year_old": False, "legacy_html_indicator": False,
                "https_status": True, "mobile_usability_proxy": True,
                "html_byte_size": 50000, "visible_text_length": 1000,
                "image_count": 5, "internal_link_count": 10, "iframe_count": 0,
                "table_count": 1, "inline_style_ratio": 0.0,
            },
        })
    return rows


def _loaded(tmp_path):
    path = tmp_path / "artifact.json"
    path.write_bytes(artifact_bytes(build_artifact(_rows())))
    return load_model(path)


def test_shadow_prediction_metadata(tmp_path):
    loaded = _loaded(tmp_path)
    result = predict_with_metadata(loaded, _rows()[0])
    assert result["prediction_status"] == "OK"
    assert result["model_version"] == "hp-content-2.2-hierarchical-logistic-experimental"
    assert result["artifact_sha"] == loaded.artifact_sha
    assert result["phase21_rank"] in "ABCD"
    assert result["phase22_rank"] in "ABCD"
    assert 0.0 <= result["phase22_score"] <= 1.0


def test_shadow_feature_builder_generates_frozen_input_schema():
    config = {
        "version": "hp-content-2-candidate",
        "weights": {
            "HTTPS": 0, "スマホviewport": 0, "Web予約導線": 1, "LINE導線": 2,
            "治療専用ページ": 0, "院長プロフィール": 1, "写真": 0,
            "料金情報": 0, "問合せCTA": 1, "SNS導線": 1, "独自LP・専門サイト": 0,
        },
        "thresholds": {"A": 6, "B": 5, "C": 2, "D": 0},
    }
    candidate = {
        "use_url": "https://example.test/",
        "features": {name: False for name in config["weights"]},
    }
    html = "<html><head><meta name='viewport' content='width=device-width'></head><body><h1>医院</h1><a href='tel:1'>電話</a></body></html>"
    row = build_features(candidate, html, config)
    assert row["p1_rank"] == "D"
    assert row["p1_score"] == 0
    assert set(row["features"]) == set(config["weights"])
    assert row["html_features"]["phone_cta"] is True
    assert row["new_features"]["page_count"] == 1


def test_missing_feature_fails_closed(tmp_path):
    loaded = _loaded(tmp_path)
    row = _rows()[0]
    del row["html_features"]["image_count"]
    with pytest.raises(ShadowValidationError, match="missing html feature"):
        predict(loaded.model, row)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "not-a-number"])
def test_invalid_numeric_fails_closed(tmp_path, bad):
    loaded = _loaded(tmp_path)
    row = _rows()[0]
    row["html_features"]["image_count"] = bad
    with pytest.raises(ShadowValidationError, match="invalid html numeric"):
        predict(loaded.model, row)


def test_invalid_artifact_fails_closed(tmp_path):
    artifact = build_artifact(_rows())
    artifact["model_version"] = "invalid"
    path = tmp_path / "invalid.json"
    path.write_bytes(artifact_bytes(artifact))
    with pytest.raises(ValueError, match="model version mismatch"):
        load_model(path)


def test_unknown_rank_fails_closed(tmp_path):
    loaded = _loaded(tmp_path)
    row = _rows()[0]
    row["p1_rank"] = "UNKNOWN"
    with pytest.raises(ShadowValidationError, match="unknown Phase2.1 rank"):
        predict(loaded.model, row)


def test_transition_aggregation():
    rows = [
        {"phase21_rank": "A", "phase22_rank": "A", "prediction_status": "OK"},
        {"phase21_rank": "A", "phase22_rank": "B", "prediction_status": "OK"},
        {"phase21_rank": "D", "phase22_rank": "C", "prediction_status": "OK"},
        {"phase21_rank": "", "phase22_rank": "", "prediction_status": "REVIEW:MISSING_HTML"},
    ]
    result = aggregate_transitions(rows)
    assert result["changed"] == 2
    assert result["unchanged"] == 1
    assert result["review"] == 1
    assert result["transition_matrix"]["A"]["B"] == 1
    assert result["transition_matrix"]["D"]["C"] == 1
    assert result["phase22_distribution"] == {"A": 1, "B": 1, "C": 1, "D": 0}


def test_nan_probability_is_never_emitted(monkeypatch, tmp_path):
    loaded = _loaded(tmp_path)
    monkeypatch.setattr(type(loaded.model.ab_model), "probability", lambda self, values: math.nan)
    with pytest.raises(ShadowValidationError, match="probability contains NaN"):
        predict_with_metadata(loaded, _rows()[0])
