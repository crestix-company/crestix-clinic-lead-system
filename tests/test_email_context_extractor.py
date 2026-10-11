"""email_context_extractor.py のunit test。HTML解析のみ(通信なし)。"""
from src.enrichment.email_research.email_context_extractor import extract_email_context


def test_extracts_surrounding_sentence_for_mailto_in_list_item():
    """実データ(sas-care.jp)の構造を再現: <li>メールでご予約の場合：<a href="mailto:...">
    のようなinline mailtoから、周辺の予約文脈を取得できること。"""
    html = (
        "<html><body><ul>"
        '<li class="img"><strong>初診の予約方法について</strong></li>'
        '<div><li>電話で予約する場合：03-0000-0000</li>'
        '<li>メールでご予約の場合：<a href="mailto:sample@clinic.example">'
        "sample@clinic.example</a>（診察日の5日前までにお願いします）</li></div>"
        "</ul></body></html>"
    )
    ctx = extract_email_context(html, "sample@clinic.example", page_title="初診予約")
    assert "予約" in ctx.nearby_text
    assert ctx.section_heading == "初診の予約方法について"
    assert "予約" in ctx.immediate_text


def test_extracts_contact_heading_for_general_inquiry():
    html = (
        "<html><body><h2>お問い合わせ</h2>"
        '<p>ご連絡は<a href="mailto:info@clinic.example">info@clinic.example</a>まで。</p>'
        "</body></html>"
    )
    ctx = extract_email_context(html, "info@clinic.example", page_title="お問い合わせ")
    assert ctx.section_heading == "お問い合わせ"


def test_returns_empty_context_when_email_not_found_on_page():
    html = "<html><body><p>フォームのみ</p></body></html>"
    ctx = extract_email_context(html, "missing@clinic.example", page_title="お問い合わせ")
    assert ctx.nearby_text == ""
    assert ctx.anchor_text == ""


def test_plain_text_email_without_mailto_still_resolves_parent_text():
    html = "<html><body><p>採用に関するお問い合わせ: sample@clinic.example までご連絡ください。</p></body></html>"
    ctx = extract_email_context(html, "sample@clinic.example", page_title="採用情報")
    assert "採用" in ctx.nearby_text


def test_nearby_text_is_capped_and_does_not_store_full_page():
    long_paragraph = "お問い合わせ" + ("あ" * 5000)
    html = f'<html><body><p>{long_paragraph}</p><a href="mailto:info@clinic.example">info@clinic.example</a></body></html>'
    ctx = extract_email_context(html, "info@clinic.example")
    assert len(ctx.nearby_text) <= 800
