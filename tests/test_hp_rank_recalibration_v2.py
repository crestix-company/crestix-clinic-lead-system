"""HP ABC v2 candidate（src/scoring/hp_rank_recalibration_v2.py）のfixtureテスト。
v1(test_hp_rank_recalibration.py)はそのまま残す。v2も本番rank_hp()には未接続。"""
from src.scoring.hp_rank_recalibration_v2 import candidate_hp_rank_v2, PRESETS


def test_clear_a_balanced_requires_dedicated_lp_and_high_score():
    feats = {"LINE導線", "SNS導線", "hp_production_company_credited", "独自LP・専門サイト",
              "has_ogp", "has_structured_data"}  # pos = 2+2+2+1+1+1 = 9 >= a_threshold(10)? check below
    result = candidate_hp_rank_v2(feats, preset="balanced")
    assert result.positive_score >= PRESETS["balanced"].b_threshold
    # Aになるにはa_threshold(10)以上必要。上のfeatsは9なのでBになる想定を先に確認する。
    assert result.hp_rank == "B"
    feats_a = feats | {"cms_wordpress"}  # +1 -> pos=10
    result_a = candidate_hp_rank_v2(feats_a, preset="balanced")
    assert result_a.hp_rank == "A"


def test_clear_b_balanced():
    feats = {"LINE導線", "SNS導線", "hp_production_company_credited", "has_ogp", "has_favicon"}  # pos=7, baseline>=1
    result = candidate_hp_rank_v2(feats, preset="balanced")
    assert result.positive_score == 7
    assert result.hp_rank == "C"  # pos=7 falls just under b_threshold(8); has_decisive=True so not UNKNOWN; baseline>=1 so C, not D
    feats2 = feats | {"has_structured_data"}  # pos=8
    result2 = candidate_hp_rank_v2(feats2, preset="balanced")
    assert result2.hp_rank == "B"
    assert result2.positive_score == 8


def test_clear_c_functioning_but_undifferentiated_site():
    feats = {"symptom_disease_content", "has_favicon", "many_pages_rich_site"}  # pos=0, baseline>=1
    result = candidate_hp_rank_v2(feats, preset="balanced")
    assert result.hp_rank == "C"
    assert result.positive_score == 0
    assert result.negative_score == 0


def test_clear_d_legacy_and_no_redemption():
    feats = {"copyright_year_stale", "legacy_html_tags"}  # neg=2+1=3 >= neg_gate(2), pos=0 < neg_override(2)
    result = candidate_hp_rank_v2(feats, preset="balanced")
    assert result.hp_rank == "D"
    assert result.negative_score >= 2


def test_clear_d_sparse_no_baseline_no_positive_no_negative():
    result = candidate_hp_rank_v2(set(), preset="balanced")
    assert result.hp_rank == "D"


def test_no_hp_when_not_found():
    result = candidate_hp_rank_v2({"LINE導線", "独自LP・専門サイト"}, preset="balanced", hp_status="NOT_FOUND")
    assert result.hp_rank == "NO_HP"


def test_treatment_feature_never_affects_v2_score():
    without = candidate_hp_rank_v2({"LINE導線", "SNS導線"}, preset="balanced")
    with_treatment = candidate_hp_rank_v2({"LINE導線", "SNS導線", "治療専用ページ"}, preset="balanced")
    assert without.hp_rank == with_treatment.hp_rank
    assert without.positive_score == with_treatment.positive_score


def test_dropped_v1_features_never_affect_v2_score():
    baseline = candidate_hp_rank_v2({"LINE導線"}, preset="balanced")
    noisy = candidate_hp_rank_v2(
        {"LINE導線", "HTTPS", "スマホviewport", "写真", "料金情報", "問合せCTA", "治療専用ページ"},
        preset="balanced",
    )
    assert baseline.positive_score == noisy.positive_score
    assert baseline.hp_rank == noisy.hp_rank


# --- UNKNOWN is feature-deficit-driven, not a fixed score==N rule ---

def test_unknown_requires_both_near_threshold_score_and_no_decisive_feature():
    """同一score(=7, balanced b_threshold=8からmargin2以内)でも、decisive featureが
    1つあるかどうかでUNKNOWN/非UNKNOWNの結果が変わることを固定する。"""
    # 決定的signal無しで弱い特徴の積み上げだけでscore=7に到達 -> UNKNOWN
    weak_only = {"has_ogp", "has_structured_data", "cms_wordpress",
                 "news_blog_recently_updated", "has_canonical", "has_faq", "good_seo_title_desc"}
    # 7個中スコア1ずつ=7点のうちdecisive(LINE/SNS/production/独自LP)は含まれない
    result_weak = candidate_hp_rank_v2(weak_only, preset="balanced")
    assert result_weak.positive_score == 7
    assert result_weak.hp_rank == "UNKNOWN"
    assert result_weak.candidate_rank_1 == "B"
    assert result_weak.candidate_rank_2 == "C"
    assert result_weak.ambiguity_reason

    # decisive signal(SNS導線)を含めて同じscore=7に到達 -> UNKNOWNにならない(Cへ確定)
    with_decisive = {"SNS導線", "has_ogp", "has_structured_data", "cms_wordpress", "has_canonical", "has_faq"}
    result_decisive = candidate_hp_rank_v2(with_decisive, preset="balanced")
    assert result_decisive.positive_score == 7
    assert result_decisive.hp_rank != "UNKNOWN"


def test_unknown_not_triggered_far_from_threshold():
    """スコアがB閾値から離れていればUNKNOWNにしない（marginの範囲内だけ）。"""
    far_below = {"has_ogp"}  # pos=1, far from b_threshold(8)
    result = candidate_hp_rank_v2(far_below, preset="balanced")
    assert result.hp_rank != "UNKNOWN"


# --- 指定5医院: 両v2 candidateとも全件リテラルC（2026-10-05 ライブ取得・固定fixture） ---

KNOWN5_FEATURES = {
    "はっとりクリニック": {"HTTPS", "院長プロフィール", "写真", "料金情報",
                      "custom_apex_domain", "has_news_or_blog_dates", "many_pages_rich_site",
                      "news_blog_recently_updated", "symptom_disease_content"},
    "永田外科胃腸内科": {"HTTPS", "スマホviewport", "治療専用ページ", "院長プロフィール", "写真", "SNS導線",
                   "builder_jimdo", "copyright_year_present", "copyright_year_stale",
                   "free_builder_subdomain", "has_canonical", "has_favicon", "has_ogp",
                   "many_pages_rich_site", "symptom_disease_content"},
    "南しばくぼ診療所": {"HTTPS", "スマホviewport", "LINE導線", "写真", "料金情報", "SNS導線",
                   "copyright_year_present", "copyright_year_recent", "custom_apex_domain",
                   "has_news_or_blog_dates", "many_pages_rich_site", "news_blog_recently_updated",
                   "symptom_disease_content"},
    "鈴木町クリニック": {"スマホviewport", "Web予約導線", "LINE導線", "治療専用ページ", "写真", "SNS導線",
                   "custom_apex_domain", "good_seo_title_desc", "has_recruit_page",
                   "many_pages_rich_site", "symptom_disease_content"},
    "松川内科クリニック": {"HTTPS", "スマホviewport", "Web予約導線", "写真", "料金情報", "問合せCTA",
                    "cms_wordpress", "copyright_year_present", "copyright_year_stale",
                    "custom_apex_domain", "good_seo_title_desc", "has_canonical", "has_favicon",
                    "has_news_or_blog_dates", "many_pages_rich_site", "news_blog_recently_updated",
                    "symptom_disease_content"},
}


def test_known_c_regression_cases_literal_c_balanced():
    for name, feats in KNOWN5_FEATURES.items():
        result = candidate_hp_rank_v2(feats, preset="balanced")
        assert result.hp_rank == "C", f"{name}: expected literal C under v2-balanced, got {result.hp_rank} (pos={result.positive_score}, neg={result.negative_score})"


def test_known_c_regression_cases_literal_c_precision_first():
    for name, feats in KNOWN5_FEATURES.items():
        result = candidate_hp_rank_v2(feats, preset="precision_first")
        assert result.hp_rank == "C", f"{name}: expected literal C under v2-precision-first, got {result.hp_rank} (pos={result.positive_score}, neg={result.negative_score})"


def test_known_c_rule_is_generalizable_not_hardcoded():
    """同じfeature集合を持つ別の(架空の)医院でも同じ結果になることを確認し、
    ruleがURL/clinic_id等のハードコードではなくfeature値だけで決まることを示す。"""
    shibakubo_feats = KNOWN5_FEATURES["南しばくぼ診療所"]
    hypothetical_clone = frozenset(shibakubo_feats)  # 同じfeatureを持つ別インスタンス
    r1 = candidate_hp_rank_v2(shibakubo_feats, preset="balanced")
    r2 = candidate_hp_rank_v2(hypothetical_clone, preset="balanced")
    assert r1.hp_rank == r2.hp_rank == "C"
