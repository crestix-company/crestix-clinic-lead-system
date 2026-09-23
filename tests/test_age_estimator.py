from copy import deepcopy
import pytest
from src.scoring.age_estimator import AgeEstimator


def test_probability_matches_independent_enumeration():
    model = AgeEstimator()
    # 2026年・1992年登録で59歳以下なら登録時年齢25歳以下。
    # 18歳入学+遅延0/1、19歳入学+遅延0に限る。尾部には依存しない。
    expected = (.365*(.855*.947 + .1*.947 + .855*.04) + .360*.855*.947)/.999
    result = model.estimate(1992, 2026)
    assert result.probability == pytest.approx(expected)
    assert result.young is True
    assert result.lower <= result.median <= result.upper
    assert sum(model.pmf.values()) == pytest.approx(1)


def test_boundary_and_exact_half():
    c = deepcopy(AgeEstimator().config)
    c["medical_school_entry_age_distribution"] = {"age_18": .5, "age_19": .5}
    c["medical_school_delay_distribution"] = {"delay_0": 1}
    c["national_exam_delay_distribution"] = {"delay_0": 1}
    result = AgeEstimator(c).estimate(1991, 2026)
    assert result.probability == .5
    assert result.young is True
    c["medical_school_entry_age_distribution"] = {"age_18": .499, "age_19": .501}
    assert AgeEstimator(c).estimate(1991, 2026).young is False


@pytest.mark.parametrize("year", ["", None, "不明", "2099", "1899"])
def test_invalid_year_is_unknown(year):
    assert AgeEstimator().estimate(year, 2026).probability is None


def test_all_old_and_recent():
    model = AgeEstimator()
    assert model.estimate(1960, 2026).probability == 0
    assert model.estimate(2026, 2026).probability == pytest.approx(1)
    assert model.estimate("平成12年", 2026).registration_year == 2000


@pytest.mark.parametrize("distribution", [{"age_18": -.1, "age_19": 1.1}, {"age_18": 0.5}, {"age_18": float("nan")}])
def test_invalid_distribution_is_rejected(distribution):
    c = deepcopy(AgeEstimator().config)
    c["medical_school_entry_age_distribution"] = distribution
    with pytest.raises(ValueError):
        AgeEstimator(c)


def test_tail_distribution_is_not_collapsed():
    model = AgeEstimator()
    assert max(model.pmf) > 40
    assert model.pmf[35+6+4+4] > 0
