"""HPランク改善Phase2 candidate（Stage1のAB/CD二値化だけを採用）のテスト。"""
from src.scoring.hp_rank_phase2 import stage1_score, phase2_candidate_rank, STAGE1_THRESHOLD
from src.utils.config import ROOT, read_config

ALL_FALSE = {
    "HTTPS": False, "スマホviewport": False, "Web予約導線": False, "LINE導線": False,
    "治療専用ページ": False, "院長プロフィール": False, "写真": False, "料金情報": False,
    "問合せCTA": False, "SNS導線": False, "独自LP・専門サイト": False,
}


def test_stage1_score_sums_only_true_features_with_candidate_weights():
    weights = read_config(ROOT / "config/hp_ranking_candidate.yml")["weights"]
    features = {**ALL_FALSE, "LINE導線": True, "SNS導線": True}
    assert stage1_score(features) == weights["LINE導線"] + weights["SNS導線"]


def test_phase2_candidate_rank_never_returns_a_or_d():
    # A/D検出は汎化しないため採用していない。B/Cの2値だけを返す設計を固定する。
    high = {**ALL_FALSE, "LINE導線": True, "SNS導線": True, "院長プロフィール": True, "問合せCTA": True}
    low = ALL_FALSE
    assert phase2_candidate_rank(high) in {"B", "C"}
    assert phase2_candidate_rank(low) in {"B", "C"}
    assert phase2_candidate_rank(high) == "B"
    assert phase2_candidate_rank(low) == "C"


def test_phase2_candidate_rank_threshold_boundary():
    weights = read_config(ROOT / "config/hp_ranking_candidate.yml")["weights"]
    # LINE導線(重み2)+SNS導線(重み1)=3。閾値5未満のためC。
    features = {**ALL_FALSE, "LINE導線": True, "SNS導線": True}
    assert stage1_score(features, weights) < STAGE1_THRESHOLD
    assert phase2_candidate_rank(features, weights) == "C"
