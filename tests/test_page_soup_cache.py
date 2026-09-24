"""Performance Phase 1: 読み取り専用HTML解析の使い回し（判定結果は変えない）。"""
from src.enrichment.hp_analysis import Page, page_soup
from src.scoring.research_scoring import treatments, hp_signals, production_companies

HTML = ("<title>テスト</title><h1>テストクリニック</h1><a href='/endoscopy/'>胃カメラ</a>"
        "<footer><a href='https://www.dr-bridge.co.jp/'>DR.BRIDGE｜クリニックホームページ制作</a></footer>")


def test_same_page_is_parsed_once():
    page = Page("https://clinic.example/", HTML)
    assert page_soup(page) is page_soup(page)


def test_cache_is_per_page_not_by_url():
    a = Page("https://clinic.example/", HTML)
    b = Page("https://clinic.example/", HTML.replace("胃カメラ", "白内障"))
    assert page_soup(a) is not page_soup(b)
    assert "白内障" in page_soup(b).get_text() and "白内障" not in page_soup(a).get_text()


def test_replaced_html_is_parsed_again():
    page = Page("https://clinic.example/", HTML)
    first = page_soup(page)
    page.html = HTML.replace("胃カメラ", "大腸カメラ")
    second = page_soup(page)
    assert second is not first and "大腸カメラ" in second.get_text()


def test_results_are_identical_with_warm_and_cold_cache():
    record = {"clinic_name": "テストクリニック", "phone": "03-0000-0001", "address": "東京都千代田区試験町1-1-1"}
    cold = [Page("https://clinic.example/", HTML)]
    warm = [Page("https://clinic.example/", HTML)]
    page_soup(warm[0])
    assert treatments(cold, record=record) == treatments(warm, record=record)
    assert hp_signals(record, cold) == hp_signals(record, warm)
    assert production_companies(cold) == production_companies(warm)
    # 判定を繰り返しても共有soupが変更されない（読み取り専用）
    assert treatments(warm, record=record) == treatments(warm, record=record)
    assert str(page_soup(warm[0])) == str(page_soup(Page("https://clinic.example/", HTML)))
