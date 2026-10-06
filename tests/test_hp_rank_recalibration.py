"""HP ABC再calibration candidate（src/scoring/hp_rank_recalibration.py）のfixtureテスト。

本番rank_hp()には未接続。既知5医院のCレグレッションケースは実URL取得結果を
feature名のsetとして固定したfixture（2026-10-05時点のライブ取得結果、
artifacts/hp_rank_recalibration/known_c_regression_cases.csvと一致）。
"""
from src.scoring.hp_rank_recalibration import candidate_hp_rank, WEIGHTS, BOUNDARY_SCORE


def test_clear_a_requires_dedicated_lp_and_high_score():
    feats = {"Web予約導線", "LINE導線", "院長プロフィール", "SNS導線", "独自LP・専門サイト"}
    result = candidate_hp_rank(feats)
    assert result.hp_rank == "A"
    assert result.hp_score == sum(WEIGHTS.values())


def test_clear_b_high_score_without_dedicated_lp():
    feats = {"LINE導線", "SNS導線", "院長プロフィール", "Web予約導線"}  # 2+1+1+1=5
    result = candidate_hp_rank(feats)
    assert result.hp_rank == "B"
    assert result.hp_score == 5


def test_clear_c_mid_score():
    feats = {"LINE導線"}  # score=2
    result = candidate_hp_rank(feats)
    assert result.hp_rank == "C"
    assert result.hp_score == 2


def test_clear_d_zero_or_one_point():
    assert candidate_hp_rank(set()).hp_rank == "D"
    assert candidate_hp_rank({"院長プロフィール"}).hp_rank == "D"  # score=1


def test_unknown_boundary_between_b_and_c():
    feats = {"LINE導線", "院長プロフィール", "SNS導線"}  # 2+1+1=4 == BOUNDARY_SCORE
    result = candidate_hp_rank(feats)
    assert result.hp_score == BOUNDARY_SCORE
    assert result.hp_rank == "UNKNOWN"
    assert result.candidate_rank_1 == "B"
    assert result.candidate_rank_2 == "C"
    assert result.ambiguity_reason  # 理由が人間に説明可能な文字列で入っている


def test_no_hp_when_not_found_regardless_of_features():
    result = candidate_hp_rank({"LINE導線", "独自LP・専門サイト"}, hp_status="NOT_FOUND")
    assert result.hp_rank == "NO_HP"
    assert result.hp_score == 0


def test_dropped_features_never_affect_score():
    """現行rank_hp()で弁別力の無かった特徴（HTTPS等）はスコアに一切寄与しない。"""
    baseline = candidate_hp_rank({"LINE導線"})
    with_dropped = candidate_hp_rank({"LINE導線", "HTTPS", "スマホviewport", "写真", "料金情報", "問合せCTA", "治療専用ページ"})
    assert baseline.hp_score == with_dropped.hp_score
    assert baseline.hp_rank == with_dropped.hp_rank


def test_treatment_presence_does_not_change_rank():
    """Treatmentカテゴリの有無（治療専用ページ特徴）はHP ABC判定に影響しない。"""
    without_treatment = candidate_hp_rank({"LINE導線", "SNS導線"})
    with_treatment = candidate_hp_rank({"LINE導線", "SNS導線", "治療専用ページ"})
    assert without_treatment.hp_rank == with_treatment.hp_rank
    assert without_treatment.hp_score == with_treatment.hp_score


# --- 指定5医院のCレグレッションケース（2026-10-05 ライブ取得・固定fixture） ---

KNOWN_C_CLINICS = {
    "はっとりクリニック": {"HTTPS", "院長プロフィール", "写真", "料金情報"},
    "永田外科胃腸内科": {"HTTPS", "スマホviewport", "治療専用ページ", "院長プロフィール", "写真", "SNS導線"},
    "南しばくぼ診療所": {"HTTPS", "スマホviewport", "LINE導線", "写真", "料金情報", "SNS導線"},
    "鈴木町クリニック": {"スマホviewport", "Web予約導線", "LINE導線", "治療専用ページ", "写真", "SNS導線"},
    "松川内科クリニック": {"HTTPS", "スマホviewport", "Web予約導線", "写真", "料金情報", "問合せCTA"},
}


def test_known_c_regression_cases_never_become_confident_a_or_b():
    for name, feats in KNOWN_C_CLINICS.items():
        result = candidate_hp_rank(feats)
        assert result.hp_rank not in ("A", "B"), f"{name} regressed to confident {result.hp_rank}"


def test_known_c_regression_breakdown_matches_expected():
    expected = {
        "はっとりクリニック": "D",
        "永田外科胃腸内科": "C",
        "南しばくぼ診療所": "C",
        "鈴木町クリニック": "UNKNOWN",
        "松川内科クリニック": "D",
    }
    for name, exp_rank in expected.items():
        result = candidate_hp_rank(KNOWN_C_CLINICS[name])
        assert result.hp_rank == exp_rank, f"{name}: expected {exp_rank}, got {result.hp_rank}"
