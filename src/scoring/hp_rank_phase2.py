"""HPランク改善 Phase2 candidate（未採用・検証専用。本番rank_hp()には未接続）。

学習240件（IS評価300件、validationはNo%5==0の60件を除く固定分割）だけで検証した結果:

Stage1（AB/CD二値、Phase1のcandidate重み+閾値5をそのまま再利用）:
  4class accuracy: 現行15.8% -> Phase2 55.4%(train)/56.7%(val)
  AB/CD accuracy : 現行30.8%(train) -> 70.0%(train)/71.7%(val)
  2段階以上ズレ  : 現行76(train) -> 2(train)/1(val)

Stage2a（AB内でのA検出）: SNS導線等で試したが、A recallを上げようとすると
  precision/accuracyが崩壊（例: 59.5%まで低下）し、A(train n=6, val n=1)は
  サンプル不足で安定した分離ができないと判断。A検出は行わず常にBを返す。

Stage2b（CD内でのD検出、copyright表示年ベース）: 学習240件ではD recall
  0%->19.4%まで改善したが、検証60件ではD recall 0%のまま(9件中0件を検出)。
  copyright年はこのGround Truthでは過学習にしかならず汎化しないため、
  Phase2 candidateには採用しない（CD内は常にCを返す）。

結果として、Phase2 candidateは「Stage1のAB/CD二値化だけを使い、各グループの
最頻ランクを返す」という単純な形が、検証データで最も安定した改善を示した。
"""
from src.utils.config import ROOT, read_config

STAGE1_THRESHOLD = 5


def stage1_score(features, weights=None):
    weights = weights or read_config(ROOT / "config/hp_ranking_candidate.yml")["weights"]
    return sum(w for name, w in weights.items() if features.get(name))


def phase2_candidate_rank(features, weights=None, threshold=STAGE1_THRESHOLD):
    """Stage1(AB/CD)だけを使う。Stage2(A検出・D検出)は汎化しないため採用しない。"""
    score = stage1_score(features, weights)
    return "B" if score >= threshold else "C"
