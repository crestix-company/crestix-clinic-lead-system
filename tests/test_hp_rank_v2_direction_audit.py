"""HP ABC candidate v2の判定方向を固定する（監査: artifacts/hp_rank_recalibration/rank_direction_audit.md）。

正式定義: A/B = Web施策をしっかり行っている医院（品質が高いほどA側）、C/D = Web施策が弱い医院
（弱い・古いほどD側）。ここではfeature値だけを使い、clinic_id/URLのハードコードは一切しない。
"""
import itertools

from src.scoring.hp_rank_recalibration_v2 import (
    candidate_hp_rank_v2, POSITIVE_WEIGHTS, NEGATIVE_WEIGHTS, EXISTING_LP, DECISIVE_FEATURES,
)

# A/B/C/D/UNKNOWNを「品質が高い=数字が小さい」順序で比較するための重み。
RANK_ORDER = {"A": 0, "B": 1, "UNKNOWN": 1.5, "C": 2, "D": 3}


def _rank_value(feats, preset="precision_first"):
    return RANK_ORDER[candidate_hp_rank_v2(feats, preset=preset).hp_rank]


def test_all_positive_features_and_no_negative_ranks_at_the_strong_end():
    """差別化要因を総動員し、劣化要因が無いfeature集合はA/B側（品質が高い側）になる。"""
    feats = set(POSITIVE_WEIGHTS) | {EXISTING_LP}
    result = candidate_hp_rank_v2(feats, preset="precision_first")
    assert result.hp_rank in ("A", "B")


def test_all_negative_features_and_no_positive_ranks_at_the_weak_end():
    """劣化要因を総動員し、差別化要因が無いfeature集合はD（品質が低い側）になる。"""
    feats = set(NEGATIVE_WEIGHTS)
    result = candidate_hp_rank_v2(feats, preset="precision_first")
    assert result.hp_rank == "D"


def test_empty_feature_set_ranks_at_the_weak_end_not_strong():
    """何も検出できない(=HPとしての機能も無い)場合に、誤ってA/B側に倒れないことを確認する。"""
    result = candidate_hp_rank_v2(set(), preset="precision_first")
    assert result.hp_rank in ("C", "D")


def test_adding_a_positive_feature_never_makes_rank_worse():
    """positive featureを1つ追加しても、rankが「悪化」方向(A/B->C/D寄り)へ動かないことを
    多数のbase集合・追加featureの組で検証する(プロパティベース、clinic固有データ不使用)。
    """
    positive_pool = sorted(POSITIVE_WEIGHTS)
    negative_pool = sorted(NEGATIVE_WEIGHTS)
    base_sets = [
        set(),
        set(positive_pool[:2]),
        set(negative_pool[:1]),
        set(positive_pool[:2]) | set(negative_pool[:1]),
    ]
    for base in base_sets:
        for extra in positive_pool:
            if extra in base:
                continue
            before = _rank_value(base)
            after = _rank_value(base | {extra})
            assert after <= before, f"adding positive feature {extra!r} made rank worse: {base} -> {base|{extra}}"


def test_adding_a_negative_feature_never_makes_rank_better():
    """negative featureを1つ追加しても、rankが「改善」方向(C/D->A/B寄り)へ動かないことを検証する。"""
    positive_pool = sorted(POSITIVE_WEIGHTS)
    negative_pool = sorted(NEGATIVE_WEIGHTS)
    base_sets = [
        set(),
        set(positive_pool[:3]),
        set(positive_pool),  # 強い土台からnegativeを足しても改善しないこと
    ]
    for base in base_sets:
        for extra in negative_pool:
            if extra in base:
                continue
            before = _rank_value(base)
            after = _rank_value(base | {extra})
            assert after >= before, f"adding negative feature {extra!r} improved rank: {base} -> {base|{extra}}"


def test_strong_site_outranks_weak_site_for_every_preset():
    """「ほぼ全差別化要因あり・劣化要因なし」のfeature集合は、
    「劣化要因のみ・差別化要因なし」のfeature集合より常に上位(A/B寄り)になる(両preset共通)。
    """
    strong = set(POSITIVE_WEIGHTS) | {EXISTING_LP}
    weak = set(NEGATIVE_WEIGHTS)
    for preset in ("balanced", "precision_first"):
        strong_rank = RANK_ORDER[candidate_hp_rank_v2(strong, preset=preset).hp_rank]
        weak_rank = RANK_ORDER[candidate_hp_rank_v2(weak, preset=preset).hp_rank]
        assert strong_rank < weak_rank


def test_decisive_feature_alone_does_not_outrank_full_positive_set():
    """決定的signal1つだけのfeature集合は、全差別化要因を持つfeature集合より
    良いrankにはならない(単調性の反証探索: 全DECISIVE_FEATURES × 他featureの組合せ)。
    """
    full_positive = set(POSITIVE_WEIGHTS) | {EXISTING_LP}
    full_rank = _rank_value(full_positive)
    for decisive in DECISIVE_FEATURES:
        partial_rank = _rank_value({decisive})
        assert full_rank <= partial_rank
