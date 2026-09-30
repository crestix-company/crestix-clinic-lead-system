from src.enrichment.hp_analysis import Page
from src.enrichment.treatment_context import (
    build_evidence_blocks,
    classify_page_type,
    detect_exclusion_context,
    detect_provider_context,
    mentions_other_facility,
)
from src.enrichment.treatment_taxonomy import OFFER_CONTEXT


def test_classify_page_type_recognizes_common_page_kinds():
    assert classify_page_type("https://clinic.example/reserve/", "ご予約", []) == "RESERVATION"
    assert classify_page_type("https://clinic.example/price.html", "料金案内", []) == "PRICE"
    assert classify_page_type("https://clinic.example/", "クリニックTOP", ["施術一覧", "外科"]) == "TREATMENT_MENU"
    assert classify_page_type("https://clinic.example/liposuction/", "脂肪吸引", []) == "TREATMENT_DETAIL"
    assert classify_page_type("https://clinic.example/medical/", "診療案内", []) == "MEDICAL_GUIDE"
    assert classify_page_type("https://clinic.example/faq/", "よくある質問", []) == "FAQ"
    assert classify_page_type("https://clinic.example/news/1", "お知らせ", []) == "NEWS"
    assert classify_page_type("https://clinic.example/blog/1", "コラム", []) == "BLOG"
    assert classify_page_type("https://clinic.example/greeting.html", "院長挨拶", []) == "DOCTOR_PROFILE"
    assert classify_page_type("https://clinic.example/research.html", "論文一覧", []) == "PUBLICATION"
    assert classify_page_type("https://clinic.example/", "TOP", []) == "HOME"
    assert classify_page_type("https://clinic.example/about-us/", "About", []) == "OTHER"


def test_build_evidence_blocks_keeps_nav_menu_and_scopes_by_heading():
    html = """
    <html><body>
    <nav>
    <h2>施術一覧</h2>
    <ul><li><h3>外科</h3><ul><li>脂肪吸引</li></ul></li></ul>
    </nav>
    <main>
    <h1>ようこそ LIVELY CLINIC へ</h1>
    <p>当院は美容外科クリニックです。</p>
    <h2>院長挨拶</h2>
    <p>第57回日本形成外科学会総会で発表しました。</p>
    </main>
    </body></html>
    """
    page = Page("https://lively.example/", html)
    blocks = build_evidence_blocks(page)
    by_path = {tuple(b["heading_path"]): b["text"] for b in blocks}
    assert ("施術一覧", "外科") in by_path
    assert "脂肪吸引" in by_path[("施術一覧", "外科")]
    assert ("ようこそ LIVELY CLINIC へ", "院長挨拶") in by_path
    assert "学会" in by_path[("ようこそ LIVELY CLINIC へ", "院長挨拶")]


def test_build_evidence_blocks_sanitizes_nul_bytes_from_raw_html_text():
    html = "<html><body><h2>見出し</h2><p>尾山台\x00\x00エリアの眼科です</p></body></html>"
    page = Page("https://clinic.example/", html)
    blocks = build_evidence_blocks(page)
    joined = " ".join(b["text"] for b in blocks) + " ".join(
        h for b in blocks for h in b["heading_path"])
    assert "\x00" not in joined
    assert "尾山台エリアの眼科です" in " ".join(b["text"] for b in blocks)


def test_mentions_other_facility_detects_group_facility_not_self():
    sentence = "白内障手術について 眼科手術について 慶翔会グループの 両国眼科 にて、当院の担当医が日帰り白内障手術を行います"
    assert mentions_other_facility(sentence, "医療法人社団　慶翔会　飯田橋眼科クリニック") is True


def test_mentions_other_facility_does_not_flag_self_reference():
    sentence = "当院にて日帰り白内障手術を行っています。"
    assert mentions_other_facility(sentence, "テスト眼科クリニック") is False


def test_mentions_other_facility_ignores_facility_name_far_from_any_verb():
    sentence = "両国眼科は近隣にあります。当院では白内障手術を実施しています。"
    # "両国眼科" appears but with no "にて/で/において" immediately after it, so no match at all.
    assert mentions_other_facility(sentence, "テスト眼科クリニック") is False


def test_detect_exclusion_context_priority_order():
    assert detect_exclusion_context("休診中です。", is_negative=False) == "CURRENTLY_SUSPENDED"
    assert detect_exclusion_context(
        "不妊専門クリニックへご紹介させていただきます。", is_negative=False) == "REFERRAL"
    assert detect_exclusion_context(
        "第57回日本形成外科学会総会で発表しました。", page_type="PUBLICATION", is_negative=False) == "PUBLICATION"
    assert detect_exclusion_context(
        "以前の勤務先での経歴です。", is_negative=False) == "DOCTOR_HISTORY"
    assert detect_exclusion_context("当院では実施しています。", is_negative=False) == "NONE"
    assert detect_exclusion_context("(negated elsewhere)", is_negative=True) == "NOT_OFFERED"


def test_detect_provider_context_maps_exclusion_and_offer_language():
    assert detect_provider_context("休診中です。", "CURRENTLY_SUSPENDED", OFFER_CONTEXT) == "NOT_PROVIDED"
    assert detect_provider_context("当院では実施しています。", "NONE", OFFER_CONTEXT) == "PROVIDED"
    assert detect_provider_context("何かの一文です。", "NONE", OFFER_CONTEXT) == "POSSIBLY_PROVIDED"
