import pytest

from src.scoring.hp_rank_phase22 import (
    CANDIDATE_STATUS, MODEL_VERSION, Phase22Model, feature_vector, fit_binary,
    phase21_baseline_rank,
)


def test_phase22_has_distinct_experimental_version():
    assert MODEL_VERSION == "hp-content-2.2-hierarchical-logistic-experimental"
    assert CANDIDATE_STATUS == "experimental-frozen"


def test_phase21_baseline_only_overrides_c_with_three_d_votes():
    row = {
        "p1_rank": "C",
        "html_features": {
            "clinic_photo_many": False,
            "doctor_photo_candidate": False,
            "html_byte_size": 50000,
            "phone_cta": True,
        },
    }
    assert phase21_baseline_rank(row) == "D"
    assert phase21_baseline_rank({**row, "p1_rank": "B"}) == "B"


def test_feature_vector_does_not_read_label_or_split():
    row = {
        "human_rank": "A", "split": "training", "features": {"HTTPS": True},
        "html_features": None, "new_features": {}, "p1_score": 1, "candidate_score": 2,
    }
    changed = {**row, "human_rank": "D", "split": "validation"}
    assert feature_vector(row, ("HTTPS",)) == feature_vector(changed, ("HTTPS",))


def test_binary_model_is_deterministic_and_separates_simple_data():
    vectors = [[-2.0], [-1.0], [1.0], [2.0]]
    targets = [0, 0, 1, 1]
    first = fit_binary(vectors, targets, steps=500)
    second = fit_binary(vectors, targets, steps=500)
    assert first == second
    assert first.probability([-1.0]) < 0.5
    assert first.probability([1.0]) > 0.5


def test_binary_model_requires_both_classes():
    with pytest.raises(ValueError, match="both classes"):
        fit_binary([[0.0], [1.0]], [1, 1])


def test_phase22_never_erases_a_phase21_d_decision():
    constant = fit_binary([[-1.0], [1.0]], [0, 1], steps=10)
    model = Phase22Model(("HTTPS",), constant, constant, constant)
    row = {
        "p1_rank": "D", "features": {"HTTPS": True}, "html_features": None,
        "new_features": {}, "p1_score": 0, "candidate_score": 0,
    }
    assert model.predict(row) == "D"
