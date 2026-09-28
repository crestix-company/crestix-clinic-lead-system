import pytest

from src.scoring.hp_rank_phase22_bounded_shadow import (
    aggregate_agreement,
    build_review_queue,
    deterministic_sample,
)


def test_deterministic_sample_is_order_independent_and_bounded():
    rows = [{"medical_key": f"key-{index}"} for index in range(600)]
    first = deterministic_sample(rows, limit=500)
    second = deterministic_sample(list(reversed(rows)), limit=500)
    assert first == second
    assert len(first) == 500


@pytest.mark.parametrize("limit", [0, 501])
def test_invalid_sample_limit_is_rejected(limit):
    with pytest.raises(ValueError, match="between 1 and 500"):
        deterministic_sample([], limit=limit)


def test_agreement_aggregation_has_requested_buckets():
    rows = [
        {"current_rank": "A", "shadow_rank": "A", "prediction_status": "OK"},
        {"current_rank": "A", "shadow_rank": "B", "prediction_status": "OK"},
        {"current_rank": "A", "shadow_rank": "C", "prediction_status": "OK"},
        {"current_rank": "A", "shadow_rank": "D", "prediction_status": "OK"},
        {"current_rank": "D", "shadow_rank": "A", "prediction_status": "OK"},
        {"current_rank": "C", "shadow_rank": "B", "prediction_status": "OK"},
        {"current_rank": "B", "shadow_rank": "", "prediction_status": "REVIEW:FEATURE_MISSING"},
    ]
    result = aggregate_agreement(rows)
    assert result["same"] == 1
    assert result["one_step_difference"] == 2
    assert result["two_step_difference"] == 1
    assert result["three_step_difference"] == 2
    assert result["ab_to_cd"] == 2
    assert result["cd_to_ab"] == 2
    assert result["a_to_d"] == 1
    assert result["d_to_a"] == 1
    assert result["feature_missing"] == 1


def test_review_queue_priority_and_limit():
    rows = [
        {"medical_key": "ad", "current_rank": "A", "shadow_rank": "D", "shadow_score": 0.2, "prediction_status": "OK"},
        {"medical_key": "cross", "current_rank": "B", "shadow_rank": "C", "shadow_score": 0.7, "prediction_status": "OK"},
        {"medical_key": "confidence", "current_rank": "C", "shadow_rank": "D", "shadow_score": 0.9, "prediction_status": "OK"},
        {"medical_key": "missing", "current_rank": "B", "shadow_rank": "", "shadow_score": "", "prediction_status": "REVIEW:FEATURE_MISSING"},
    ]
    queue = build_review_queue(rows)
    assert [row["review_reason"] for row in queue] == [
        "A_D_transition", "AB_CD_transition", "high_confidence_disagreement", "feature_missing",
    ]
    assert len(build_review_queue(rows * 20, limit=50)) == 50


def test_review_queue_does_not_include_same_rank():
    rows = [{"medical_key": "same", "current_rank": "B", "shadow_rank": "B", "shadow_score": 0.99, "prediction_status": "OK"}]
    assert build_review_queue(rows) == []
