"""email_extraction.py のpure function群に対するfixtureベースのunit test。

HTTPアクセスは一切行わない。全てのテストはHTML/テキスト文字列のfixtureを直接渡す。
"""
from src.enrichment.email_research.email_extraction import (
    classify_email_type,
    dedupe_emails,
    detect_forms,
    extract_all_emails,
    extract_mailto_emails,
    extract_plaintext_emails,
    extract_structured_data_emails,
    is_valid_email,
    looks_like_other_facility,
    normalize_email_text,
)


def test_extract_mailto_basic():
    html = '<a href="mailto:info@example-clinic.jp">お問い合わせ</a>'
    results = extract_mailto_emails(html)
    assert len(results) == 1
    assert results[0].email == "info@example-clinic.jp"
    assert results[0].extraction_method == "MAILTO"


def test_extract_mailto_with_query_parameters():
    html = '<a href="mailto:yoyaku@example-clinic.jp?subject=予約希望&body=お名前">予約はこちら</a>'
    results = extract_mailto_emails(html)
    assert len(results) == 1
    assert results[0].email == "yoyaku@example-clinic.jp"


def test_extract_plaintext_email():
    text = "お問い合わせは info@sample-dental.jp までご連絡ください。"
    results = extract_plaintext_emails(text)
    assert [r.email for r in results] == ["info@sample-dental.jp"]
    assert results[0].extraction_method == "PAGE_TEXT"


def test_extract_structured_data_email():
    html = """
    <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"MedicalOrganization",
     "name":"サンプルクリニック","email":"contact@sample-clinic.jp"}
    </script>
    """
    results = extract_structured_data_emails(html)
    assert len(results) == 1
    assert results[0].email == "contact@sample-clinic.jp"
    assert results[0].extraction_method == "STRUCTURED_DATA"


def test_extract_structured_data_nested_email():
    html = """
    <script type="application/ld+json">
    {"@type":"MedicalOrganization","contactPoint":{"@type":"ContactPoint","email":"nested@sample-clinic.jp"}}
    </script>
    """
    results = extract_structured_data_emails(html)
    assert [r.email for r in results] == ["nested@sample-clinic.jp"]


def test_full_width_at_sign_is_normalized():
    assert normalize_email_text("info＠example-clinic.jp") == "info@example-clinic.jp"
    text = "ご連絡は info＠example-clinic.jp まで。"
    results = extract_plaintext_emails(text)
    assert [r.email for r in results] == ["info@example-clinic.jp"]


def test_html_entity_at_sign_is_decoded():
    text = "ご連絡は info&#64;example-clinic.jp まで。"
    results = extract_plaintext_emails(text)
    assert [r.email for r in results] == ["info@example-clinic.jp"]


def test_invalid_email_rejected():
    assert is_valid_email("not-an-email") is False
    assert is_valid_email("@example.jp") is False
    assert is_valid_email("info@") is False
    assert is_valid_email("info@example.jp.png") is False  # 画像パス誤検出


def test_dummy_address_rejected():
    assert is_valid_email("test@example.com") is False
    assert is_valid_email("noreply@sample-clinic.jp") is False
    assert is_valid_email("dummy@xxx.jp") is False


def test_duplicate_normalization_case_insensitive():
    html = (
        '<a href="mailto:Info@Example-Clinic.jp">問い合わせ</a>'
        '<a href="mailto:info@example-clinic.jp">予約</a>'
    )
    extracted = extract_mailto_emails(html)
    deduped = dedupe_emails(extracted)
    assert len(deduped) == 1
    assert deduped[0].email == "Info@Example-Clinic.jp"  # 最初に見つかった表記を残す


def test_extract_all_emails_priority_and_dedupe():
    html = """
    <html><body>
    <a href="mailto:info@sample-clinic.jp">お問い合わせ</a>
    <script type="application/ld+json">{"email":"info@sample-clinic.jp"}</script>
    <p>その他のご連絡先: info@sample-clinic.jp</p>
    </body></html>
    """
    all_emails = extract_all_emails(html)
    deduped = dedupe_emails(all_emails)
    assert len(deduped) == 1
    assert deduped[0].extraction_method == "MAILTO"  # mailtoが最優先で残る


def test_classify_email_type_reservation():
    assert classify_email_type("yoyaku@example-clinic.jp") == "RESERVATION"
    assert classify_email_type("info@example-clinic.jp", context_hint="ご予約フォーム") == "RESERVATION"


def test_classify_email_type_recruit_only():
    assert classify_email_type("saiyo@example-clinic.jp") == "RECRUIT"
    assert classify_email_type("info@example-clinic.jp", context_hint="採用情報 募集要項") == "RECRUIT"


def test_classify_email_type_general():
    assert classify_email_type("info@example-clinic.jp") == "GENERAL"


def test_classify_email_type_unknown_when_no_hint():
    assert classify_email_type("kenji.yamada@example-clinic.jp") == "UNKNOWN"


def test_detect_form_only_reservation():
    html = """
    <h2>ご予約はこちら</h2>
    <form action="/reserve/submit">
      <input name="name"><input name="date"><button>予約する</button>
    </form>
    """
    forms = detect_forms(html)
    assert len(forms) == 1
    assert forms[0].form_type == "RESERVATION"


def test_detect_form_only_recruit():
    html = """
    <h2>採用情報</h2>
    <form action="/recruit/apply">
      <input name="name"><button>応募する</button>
    </form>
    """
    forms = detect_forms(html)
    assert forms[0].form_type == "RECRUIT"


def test_detect_form_general_contact():
    html = '<h3>お問い合わせ</h3><form action="/contact"><input name="msg"></form>'
    forms = detect_forms(html)
    assert forms[0].form_type == "GENERAL"


def test_no_email_and_no_form_returns_empty():
    html = "<html><body><p>当院のご案内ページです。</p></body></html>"
    assert extract_all_emails(html) == []
    assert detect_forms(html) == []


def test_looks_like_other_facility_flags_mismatch():
    evidence = "医療法人社団〇〇会 別の歯科医院 アクセスのご案内"
    assert looks_like_other_facility(evidence, "さくら眼科クリニック") is True


def test_looks_like_other_facility_no_flag_when_name_present():
    evidence = "さくら眼科クリニックへのアクセスはこちら"
    assert looks_like_other_facility(evidence, "さくら眼科クリニック") is False
