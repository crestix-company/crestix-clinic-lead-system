import csv
from src.outreach.article_recommender import read_master, recommend


def test_csv_origin_and_preference(tmp_path):
    path = tmp_path / "articles.csv"
    rows = [
        {"No.": "1", "タイトル": "東京都の内科おすすめ", "URL": "https://clinic.mynavi.jp/article/tokyo_internal/", "KW": "東京都 内科"},
        {"No.": "2", "タイトル": "世田谷区の内科おすすめ", "URL": "https://clinic.mynavi.jp/article/setagaya_internal/", "KW": "世田谷区 内科"},
        {"No.": "3", "タイトル": "世田谷区の内視鏡検査", "URL": "https://clinic.mynavi.jp/article/setagaya_endoscopy/", "KW": "世田谷区 内視鏡検査"},
    ]
    with path.open("w", encoding="utf-8", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=["No.", "タイトル", "URL", "KW"])
        w.writeheader()
        w.writerows(rows)
    source = read_master(path)
    result = recommend(source, prefecture="東京都", municipality="世田谷区",
                       departments=("内科",), treatments=("内視鏡検査",))
    assert result[0].article.url == rows[2]["URL"]
    assert len(result) == 3


def test_no_unrelated_fallback():
    from src.outreach.article_recommender import Article
    articles = [Article("渋谷区の眼科", "https://clinic.mynavi.jp/article/example/", "渋谷区 眼科", "1")]
    assert recommend(articles, prefecture="東京都", municipality="杉並区", departments=("皮膚科",)) == []


def test_invalid_urls_never_used(tmp_path):
    path = tmp_path / "articles.csv"
    with path.open("w", encoding="utf-8", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=["No.", "タイトル", "URL", "KW"])
        w.writeheader()
        w.writerow({"No.":"1","タイトル":"内科","URL":"https://clinic.mynavi.jp.evil.invalid/article/x","KW":"内科"})
    assert read_master(path) == []


# --- 地域優先順位(2026-10-13 実データ検証で発見した回帰ケース) ---
from src.outreach.article_recommender import Article  # noqa: E402


def test_other_municipality_in_same_prefecture_never_outranks_generic_prefecture_article():
    """小金井市/中央区ケース: 医院と異なる市区町村限定の記事は、都道府県ボーナスを得られず、
    同一都道府県の地域限定なし記事より上位にならない。"""
    generic = Article("東京都の眼科クリニックおすすめ10選", "https://clinic.mynavi.jp/article/tokyo_ophthalmology/",
                      "東京 眼科", "10")
    other_ward = Article("東京都中央区の眼科クリニックおすすめ10選", "https://clinic.mynavi.jp/article/chuoku_ophthalmology/",
                         "中央区 眼科", "5")  # source_noが小さく、スコア同点なら従来ロジックではこちらが先頭になっていた
    result = recommend([generic, other_ward], prefecture="東京都", municipality="小金井市", departments=("眼科",))
    assert result[0].article.url == generic.url
    assert "都道府県一致" in result[0].reasons
    other = next(r for r in result if r.article.url == other_ward.url)
    assert "都道府県一致" not in other.reasons and "市区町村一致" not in other.reasons
    assert other.score < result[0].score


def test_own_municipality_article_still_wins_over_generic_prefecture_article():
    """市区町村限定記事は、その市区町村に一致する医院には引き続き最上位で自動推薦される。"""
    generic = Article("東京都の眼科クリニックおすすめ10選", "https://clinic.mynavi.jp/article/tokyo_ophthalmology/",
                      "東京 眼科", "10")
    own_ward = Article("中央区の眼科クリニックおすすめ10選", "https://clinic.mynavi.jp/article/chuoku_ophthalmology/",
                       "中央区 眼科", "5")
    result = recommend([generic, own_ward], prefecture="東京都", municipality="中央区", departments=("眼科",))
    assert result[0].article.url == own_ward.url
    assert "市区町村一致" in result[0].reasons


def test_prefecture_match_outranks_stronger_treatment_score_from_other_prefecture():
    """千代田区/埼玉県ケース: 治療一致(スコア100)でも、都道府県外の記事は同一都道府県の
    標榜科一致記事より上位にしない。"""
    out_of_prefecture = Article("埼玉県のおすすめ糖尿病内科クリニック10選",
                                "https://clinic.mynavi.jp/article/saitamaken_diabetes-clinic/",
                                "埼玉県 糖尿病内科", "1")
    in_prefecture = Article("東京都の内科クリニックおすすめ10選", "https://clinic.mynavi.jp/article/tokyo_internal/",
                            "東京都 内科", "20")
    result = recommend([out_of_prefecture, in_prefecture], prefecture="東京都", municipality="千代田区",
                       departments=("内科", "糖尿病内科"), treatments=("糖尿病",))
    assert result[0].article.url == in_prefecture.url
    assert "都道府県一致" in result[0].reasons


def test_out_of_prefecture_still_usable_as_last_resort_when_no_local_candidate():
    """都道府県内に候補が無い場合は、都道府県外の記事が最後の手段として残る
    (呼び出し側 generate_one() の地域不一致チェックでNEEDS_REVIEWになる設計、recommend()単体では除外しない)。"""
    out_of_prefecture = Article("札幌市の眼科クリニックおすすめ10選", "https://clinic.mynavi.jp/article/sapporo_ophthalmology/",
                                "札幌 眼科", "5")
    result = recommend([out_of_prefecture], prefecture="東京都", municipality="世田谷区", departments=("眼科",))
    assert len(result) == 1 and result[0].article.url == out_of_prefecture.url
    assert "都道府県一致" not in result[0].reasons and "市区町村一致" not in result[0].reasons
