"""HP ABC判定 再calibration v2 candidate（未接続・Dry Run用。v1=hp_rank_recalibration.pyは変更せず残す）。

v1（既存rank_hp()の11特徴量のうち弁別力のある5特徴だけを使う単純加点式）はRecallが
22.2%まで落ち込み、「A/Bをほぼ消してしまう」過修正だったため不採用。v2では、既存
signalの再評価（artifacts/hp_rank_recalibration/feature_discriminative_power.csv）に加え、
HTML再取得で得られる新しい弁別特徴（HP制作会社credit・OGP・構造化データ・CMS種別・
copyright年の鮮度・お知らせ更新鮮度・独自ドメインか等）を導入し、Precision/Recallの
両方をv1より大きく改善する。Treatmentカテゴリ関連の特徴は使わない（HP ABC判定に
Treatmentの有無・内容を一切混ぜない）。

スコアは2軸に分離する:

  POSITIVE_SCORE: 差別化要因（LINE導線・SNS導線・HP制作会社credit・OGP・構造化データ・
                  WordPress・院長プロフィール・Web予約導線・canonical・お知らせ更新鮮度・
                  独自LP・専門サイト・SEOタイトル/descriptionの質・FAQ）。B/Aを駆動する。
  NEGATIVE_SCORE: 劣化要因（copyright年が古い・legacy HTMLタグ(font/center/marquee/blink)・
                  リンクされたstylesheetが無い・お知らせが古いまま更新停止・1ページ構成・
                  Flash/appletの残存・無料ビルダーの共有サブドメイン運用）。Dへ押し下げる。
  BASELINE_COUNT: 症状/疾患ページの充実・favicon・canonical・多ページ構成・お知らせ/ブログの
                  存在、のうち該当する個数。NEGATIVE_SCOREが無く、POSITIVE_SCOREも閾値未満
                  だが「実在する機能しているHP」であることを示す下限（C/Dの境界に使う）。

判定順序（優先度の高い順）:
  1. NEGATIVE_SCORE>=NEG_GATE かつ POSITIVE_SCORE<NEG_OVERRIDE  -> D（明確に古い/停止していて、
     救済できるだけの差別化要因も無い）
  2. POSITIVE_SCORE>=B_THRESHOLD -> B（独自LP・専門サイトがあり、かつPOSITIVE_SCORE>=A_THRESHOLD
     ならA）
  3. B_THRESHOLD-MARGIN <= POSITIVE_SCORE < B_THRESHOLD かつ「決定的signal」(DECISIVE_FEATURES)が
     1つも無い -> UNKNOWN(B,C)。単純な「score==N」ではなく、しきい値に近いスコアで、かつ
     それを押し切れるだけの強いsignalが無い場合だけに限定する（同じスコアでも決定的signalが
     1つでもあればUNKNOWN化しない。test_hp_rank_recalibration_v2.pyで固定）。
  4. BASELINE_COUNT>=BASELINE_GATE -> C（機能しているHPだが差別化要因が無い）
  5. それ以外 -> D
"""
from dataclasses import dataclass

# --- 既存rank_hp()の特徴のうち、2026年時点でも弁別力が残っていた5つ ---
EXISTING_POSITIVE = {"LINE導線": 2, "SNS導線": 2, "院長プロフィール": 1, "Web予約導線": 1}
EXISTING_LP = "独自LP・専門サイト"

# --- 新規特徴（live HTML再取得で追加検出。Treatment関連は含まない） ---
NEW_POSITIVE = {
    "hp_production_company_credited": 2,  # OR最大(7.78)。制作会社クレジットがある=プロ制作の強い根拠
    "has_ogp": 1, "has_structured_data": 1, "cms_wordpress": 1, "has_canonical": 1,
    "news_blog_recently_updated": 1, "good_seo_title_desc": 1, "has_faq": 1,
}
POSITIVE_WEIGHTS = {**EXISTING_POSITIVE, EXISTING_LP: 1, **NEW_POSITIVE}

NEGATIVE_WEIGHTS = {
    "copyright_year_stale": 2, "legacy_html_tags": 1, "no_linked_stylesheet": 1,
    "news_blog_stale": 1, "single_page_or_near_single_page_site": 1, "flash_or_applet": 1,
    "builder_wix": 2, "builder_jimdo": 1,
}
# 無料ビルダーの共有サブドメイン運用（custom_apex_domainが無い場合のみ負点。
# 無料ビルダーでも独自ドメイン化していれば本体のビルダー検出signalだけで評価する）。
FREE_BUILDER_SUBDOMAIN_PENALTY = 2

BASELINE_FEATURES = {"symptom_disease_content", "has_favicon", "has_canonical",
                     "many_pages_rich_site", "has_news_or_blog_dates"}

# 閾値に近いスコアでも、これらのうち1つでも存在すれば「決定的signalあり」とみなし
# UNKNOWN化しない（=弱い特徴の積み上げだけで閾値に近づいたケースだけをUNKNOWNにする）。
DECISIVE_FEATURES = {"LINE導線", "SNS導線", "hp_production_company_credited", EXISTING_LP}

# v1で弁別力が無かったことを確認済みの特徴。v2でも重み0のまま（治療専用ページを含む）。
DROPPED_FEATURES = ("HTTPS", "スマホviewport", "写真", "料金情報", "問合せCTA", "治療専用ページ")


@dataclass(frozen=True)
class CandidateRankResultV2:
    hp_rank: str
    positive_score: int
    negative_score: int
    candidate_rank_1: str | None = None
    candidate_rank_2: str | None = None
    score_1: int | None = None
    score_2: int | None = None
    ambiguity_reason: str | None = None


@dataclass(frozen=True)
class RuleParams:
    name: str
    b_threshold: int
    a_threshold: int
    margin: int
    neg_gate: int
    neg_override: int
    baseline_gate: int


# 2026-10-05 Gold 291件（生HTML再取得・学習/検証split）でのグリッドサーチにより選定。
# 詳細はartifacts/hp_rank_recalibration/hp_rank_v2_findings.mdとcandidate_comparison.csv参照。
PRESETS = {
    "balanced": RuleParams("v2-balanced", b_threshold=8, a_threshold=10, margin=2,
                            neg_gate=2, neg_override=2, baseline_gate=1),
    "precision_first": RuleParams("v2-precision-first", b_threshold=10, a_threshold=12, margin=2,
                                   neg_gate=2, neg_override=2, baseline_gate=1),
}


def compute_scores(feature_names):
    names = set(feature_names or ())
    pos = sum(POSITIVE_WEIGHTS.get(n, 0) for n in names)
    neg = sum(NEGATIVE_WEIGHTS.get(n, 0) for n in names)
    if "free_builder_subdomain" in names and "custom_apex_domain" not in names:
        neg += FREE_BUILDER_SUBDOMAIN_PENALTY
    baseline = sum(1 for n in BASELINE_FEATURES if n in names)
    has_dedicated_lp = EXISTING_LP in names
    has_decisive = bool(names & DECISIVE_FEATURES)
    return pos, neg, baseline, has_dedicated_lp, has_decisive


def _ambiguity_reason(names, pos, b_threshold):
    missing_decisive = sorted(DECISIVE_FEATURES - names)
    gap = b_threshold - pos
    return (
        f"B閾値({b_threshold})にscore差{gap}で近接(score={pos})しているが、"
        f"決定的signal({'/'.join(missing_decisive)})のいずれも検出できず、"
        "弱いsignalの積み上げだけでは自信を持ってB/Cを確定できないため人間レビュー推奨。"
    )


def candidate_hp_rank_v2(feature_names, *, preset="balanced", hp_status=None):
    """feature_names: 既存rank_hp()のfeature名 + 新規feature名（extract_features系）の集合。

    hp_status=='NOT_FOUND'のときだけNO_HP。それ以外は2軸スコア(POSITIVE/NEGATIVE)と
    BASELINE_COUNTから A/B/C/D/UNKNOWN を決定する。Treatment関連の特徴は一切参照しない。
    """
    if hp_status == "NOT_FOUND":
        return CandidateRankResultV2(hp_rank="NO_HP", positive_score=0, negative_score=0)

    params = PRESETS[preset]
    names = set(feature_names or ())
    pos, neg, baseline, has_lp, has_decisive = compute_scores(names)

    if neg >= params.neg_gate and pos < params.neg_override:
        return CandidateRankResultV2(hp_rank="D", positive_score=pos, negative_score=neg)

    if pos >= params.b_threshold:
        rank = "A" if (has_lp and pos >= params.a_threshold) else "B"
        return CandidateRankResultV2(hp_rank=rank, positive_score=pos, negative_score=neg)

    if (params.b_threshold - params.margin) <= pos < params.b_threshold and not has_decisive:
        reason = _ambiguity_reason(names, pos, params.b_threshold)
        return CandidateRankResultV2(
            hp_rank="UNKNOWN", positive_score=pos, negative_score=neg,
            candidate_rank_1="B", candidate_rank_2="C", score_1=pos, score_2=pos,
            ambiguity_reason=reason,
        )

    if baseline >= params.baseline_gate:
        return CandidateRankResultV2(hp_rank="C", positive_score=pos, negative_score=neg)

    return CandidateRankResultV2(hp_rank="D", positive_score=pos, negative_score=neg)
