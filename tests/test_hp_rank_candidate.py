"""HPランク改善 Phase1: rank_hp()のリファクタリングが既存動作を変えていないこと、
および config/hp_ranking_candidate.yml（未採用のcandidate）の形式を検証する。

candidate configはどのコードパスからも呼ばれない（analyze()はconfig=Noneのまま
config/hp_ranking.ymlのみを使う）。本番の判定ロジック・重み・閾値はここでは変更しない。
"""
from src.scoring.research_scoring import hp_rank_features, hp_rank_score, hp_rank_from_score, rank_hp
from src.utils.config import ROOT, read_config


def test_hp_rank_from_score_boundaries():
    thresholds = {"A": 12, "B": 8, "C": 4, "D": 0}
    assert hp_rank_from_score(12, thresholds) == "A"
    assert hp_rank_from_score(11, thresholds) == "B"
    assert hp_rank_from_score(8, thresholds) == "B"
    assert hp_rank_from_score(7, thresholds) == "C"
    assert hp_rank_from_score(4, thresholds) == "C"
    assert hp_rank_from_score(3, thresholds) == "D"
    assert hp_rank_from_score(0, thresholds) == "D"


def test_hp_rank_score_sums_only_true_features():
    weights = {"HTTPS": 1, "スマホviewport": 1, "写真": 5}
    features = {"HTTPS": True, "スマホviewport": False, "写真": True}
    score, reasons = hp_rank_score(features, weights)
    assert score == 6
    assert {r["feature"] for r in reasons} == {"HTTPS", "写真"}


def test_production_config_still_current_weights_and_thresholds():
    # 本番configは今回のPhase1で変更していないことを固定する回帰テスト。
    cfg = read_config(ROOT / "config/hp_ranking.yml")
    assert cfg["version"] == "hp-content-1"
    assert cfg["weights"] == {
        "HTTPS": 1, "スマホviewport": 1, "Web予約導線": 2, "LINE導線": 1, "治療専用ページ": 3,
        "院長プロフィール": 2, "写真": 1, "料金情報": 1, "問合せCTA": 1, "SNS導線": 1, "独自LP・専門サイト": 2,
    }
    assert cfg["thresholds"] == {"A": 12, "B": 8, "C": 4, "D": 0}


def test_candidate_config_loads_and_is_not_wired_into_default_rank_hp():
    cfg = read_config(ROOT / "config/hp_ranking_candidate.yml")
    assert cfg["version"] == "hp-content-2-candidate"
    assert set(cfg["weights"]) == {
        "HTTPS", "スマホviewport", "Web予約導線", "LINE導線", "治療専用ページ", "院長プロフィール",
        "写真", "料金情報", "問合せCTA", "SNS導線", "独自LP・専門サイト",
    }
    assert cfg["thresholds"] == {"A": 6, "B": 5, "C": 2, "D": 0}

    # rank_hp()をconfig省略で呼んだときは、必ず本番config（hp-content-1）が使われる。
    class Page:
        url = "https://example.test/"
        html = "<html><head></head><body></body></html>"
        main_text = ""
        links = []

    result = rank_hp([Page()], {"treatment_evidence": []}, [])
    assert result["hp_rank_version"] == "hp-content-1"


def test_rank_hp_end_to_end_matches_manual_feature_and_score_computation():
    class Page:
        url = "https://example.test/"
        html = (
            '<html><head><meta name="viewport" content="width=device-width"></head>'
            '<body><img src="a.jpg"><img src="b.jpg"><img src="c.jpg">'
            '<a href="https://line.me/R/ti/p/@abc">LINE</a>'
            '<a href="tel:0300000000">お問い合わせ</a></body></html>'
        )
        main_text = "料金 3000円（税込）"
        links = [
            {"url": "https://line.me/R/ti/p/@abc", "text": "LINE"},
            {"url": "tel:0300000000", "text": "お問い合わせ"},
        ]

    page = Page()
    features = hp_rank_features([page], {"treatment_evidence": []}, [])
    assert features["HTTPS"] is True
    assert features["スマホviewport"] is True
    assert features["LINE導線"] is True
    assert features["写真"] is True
    assert features["料金情報"] is True
    assert features["問合せCTA"] is True
    assert features["Web予約導線"] is False
    assert features["SNS導線"] is False

    result = rank_hp([page], {"treatment_evidence": []}, [])
    assert result["hp_score"] == sum(r["points"] for r in result["hp_rank_reasons"])
    assert result["hp_rank"] in {"A", "B", "C", "D"}
