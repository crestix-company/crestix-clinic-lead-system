"""HP ABC判定 再calibration candidate（未接続・Dry Run用）。

本番のrank_hp()（research_scoring.py）は変更しない。既存の11特徴量チェックリストのうち、
2026年時点でほぼ全HPが満たしてしまい弁別力を失った項目（HTTPS/スマホviewport/写真/
料金情報/問合せCTA/治療専用ページ）の重みを0にし、人間評価300件で実際に弁別力が
残っていた項目（Web予約導線/LINE導線/院長プロフィール/SNS導線/独自LP・専門サイト）
だけでスコアする。詳細は artifacts/hp_rank_recalibration/summary.md 参照。

score==4（B/Cの境界）はUNKNOWNとして候補ランク2件・差分理由を返す（section 13の
説明可能UNKNOWN要件）。これは「わからない」の放置ではなく、人間レビュー待ちの
明示的な保留状態。
"""
from dataclasses import dataclass

WEIGHTS = {
    "Web予約導線": 1,
    "LINE導線": 2,
    "院長プロフィール": 1,
    "SNS導線": 1,
    "独自LP・専門サイト": 1,
}
# 現行rank_hp()の特徴量のうち、人間評価300件で弁別力が確認できなかったため
# このcandidateでは重み0（スコアに寄与しない）。
DROPPED_FEATURES = ("HTTPS", "スマホviewport", "写真", "料金情報", "問合せCTA", "治療専用ページ")

MAX_SCORE = sum(WEIGHTS.values())
BOUNDARY_SCORE = 4  # B/Cの境界。このスコアだけはUNKNOWN(B/C)として保留する。


@dataclass(frozen=True)
class CandidateRankResult:
    hp_rank: str
    hp_score: int
    candidate_rank_1: str | None = None
    candidate_rank_2: str | None = None
    ambiguity_reason: str | None = None


def candidate_hp_rank(feature_names, *, hp_status=None):
    """feature_names: rank_hp()のhp_rank_reasonsから得たfeature名のset/iterable。

    hp_status=="NOT_FOUND"（公式HP自体が見つからない）のときだけNO_HPを返す。
    それ以外は既存rank_hp()と同じ入力形式（feature名の集合）からA/B/C/D/UNKNOWNを返す。
    """
    if hp_status == "NOT_FOUND":
        return CandidateRankResult(hp_rank="NO_HP", hp_score=0)

    names = set(feature_names or ())
    score = sum(WEIGHTS.get(name, 0) for name in names)
    has_dedicated_lp = "独自LP・専門サイト" in names

    if score <= 1:
        return CandidateRankResult(hp_rank="D", hp_score=score)
    if score <= 3:
        return CandidateRankResult(hp_rank="C", hp_score=score)
    if score == BOUNDARY_SCORE:
        present = sorted(n for n in names if n in WEIGHTS)
        reason = f"境界値(score={score}): 有効signal={present or ['無し']}。B/Cの分岐点で人間レビュー推奨。"
        return CandidateRankResult(hp_rank="UNKNOWN", hp_score=score,
                                    candidate_rank_1="B", candidate_rank_2="C", ambiguity_reason=reason)
    # score >= 5
    if has_dedicated_lp:
        return CandidateRankResult(hp_rank="A", hp_score=score)
    return CandidateRankResult(hp_rank="B", hp_score=score)
