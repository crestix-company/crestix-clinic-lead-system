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
