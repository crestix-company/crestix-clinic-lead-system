"""email_role_classifier.py のunit test。純粋関数のみ(HTTP/API接続は一切行わない)。"""
import json
from pathlib import Path

import pytest
from src.enrichment.email_research.email_role_classifier import (
    ROLE_GENERAL,
    ROLE_RECRUIT,
    ROLE_RESERVATION,
    SALES_NO,
    SALES_YES,
    classify_email_role,
)


def test_case1_info_local_part_is_general_and_sales_usable():
    result = classify_email_role("info@clinic.jp")
    assert result.role == ROLE_GENERAL
    assert result.sales_usable == SALES_YES


def test_case2_recruit_local_part_is_recruit_and_not_sales_usable():
    result = classify_email_role("recruit@clinic.jp")
    assert result.role == ROLE_RECRUIT
    assert result.sales_usable == SALES_NO


def test_case3_yoyaku_local_part_is_reservation_and_not_sales_usable():
    result = classify_email_role("yoyaku@clinic.jp")
    assert result.role == ROLE_RESERVATION
    assert result.sales_usable == SALES_NO


def test_case4_freemail_with_general_context_is_general_and_sales_usable():
    """医院公式HPに「お問い合わせ example@gmail.com」と明記 -> フリーメールでもGENERAL/YES。"""
    result = classify_email_role(
        "example@gmail.com",
        context_text="医院公式HP お問い合わせ example@gmail.com までご連絡ください",
    )
    assert result.role == ROLE_GENERAL
    assert result.sales_usable == SALES_YES


def test_case5_freemail_on_recruit_page_is_recruit_and_not_sales_usable():
    """採用ページに example@gmail.com -> フリーメールでもRECRUIT/NO(ドメインだけで判定しない)。"""
    result = classify_email_role(
        "example@gmail.com",
        page_url="https://clinic.example/recruit/",
        page_title="採用情報 | サンプルクリニック",
    )
    assert result.role == ROLE_RECRUIT
    assert result.sales_usable == SALES_NO


def test_specific_role_keyword_wins_over_general_keyword_in_same_context():
    """「採用のお問い合わせ」のように具体的用途とGENERALキーワードが同時に出現する場合、
    具体的用途(RECRUIT)を優先する。"""
    result = classify_email_role(
        "office@clinic.jp", context_text="採用のお問い合わせはこちら",
    )
    assert result.role == ROLE_RECRUIT


def test_context_text_outranks_local_part():
    """優先順位1(周辺文脈)が優先順位4(local-part)より優先される
    (local-partは一般的な"info"だが、文脈が"予約"ならRESERVATIONになる)。"""
    result = classify_email_role("info@clinic.jp", context_text="ご予約専用のメールです")
    assert result.role == ROLE_RESERVATION


def test_office_local_part_classified_as_office():
    result = classify_email_role("jimu@clinic.jp")
    assert result.role == "OFFICE"
    assert result.sales_usable == SALES_YES


def test_medical_cooperation_context_not_sales_usable():
    result = classify_email_role(
        "renkei@clinic.jp", context_text="地域連携室(紹介患者専用)のご連絡先です",
    )
    assert result.role == "MEDICAL_COOPERATION"
    assert result.sales_usable == SALES_NO


def test_personal_context_requires_explicit_statement():
    result = classify_email_role("doctor1@clinic.jp", context_text="院長直通のメールアドレスです")
    assert result.role == "PERSONAL"
    assert result.sales_usable == "REVIEW"


def test_no_signal_at_all_is_unknown_and_review():
    result = classify_email_role("zaidan@jptoho.or.jp")
    assert result.role == "UNKNOWN"
    assert result.sales_usable == "REVIEW"


def test_freemail_domain_alone_never_forces_unknown_or_rejection():
    """ドメイン(gmail等)だけを理由にUNKNOWNや除外にはしない
    (文脈が無ければUNKNOWNになるが、それはdomainが理由ではなく判定材料不足が理由)。"""
    with_context = classify_email_role("x@gmail.com", context_text="お問い合わせはこちら")
    without_context = classify_email_role("x@gmail.com")
    assert with_context.role == ROLE_GENERAL
    assert without_context.role == "UNKNOWN"  # local-part "x"に手がかりが無いだけ


def test_general_from_section_heading():
    """Hardening Phase 2: section_heading(見出し)からGENERALを判定できる。"""
    result = classify_email_role(
        "info@clinic.jp", section_heading="お問い合わせ", nearby_text="ご連絡はこちらまで",
    )
    assert result.role == ROLE_GENERAL
    assert result.role_confidence >= 0.9


def test_recruit_from_nearby_context_even_with_freemail():
    """Hardening Phase 2 + Case5相当: nearby_textに採用文脈があればGmailでもRECRUIT。"""
    result = classify_email_role(
        "example@gmail.com", nearby_text="採用情報 スタッフ募集のお問い合わせはこちらまで",
    )
    assert result.role == ROLE_RECRUIT
    assert result.sales_usable == "NO"


def test_reservation_from_nearby_context_even_with_freemail():
    """実データ(御茶ノ水呼吸ケアクリニック)相当: nearby_textに予約文脈があればGmailでもRESERVATION。"""
    result = classify_email_role(
        "example@gmail.com", section_heading="初診の予約方法について",
        nearby_text="メールでご予約の場合：example@gmail.com（診察日の5日前までにお願いします）",
    )
    assert result.role == "RESERVATION"
    assert result.sales_usable == "NO"


def test_general_from_contact_section_with_freemail():
    result = classify_email_role(
        "example@gmail.com", section_heading="お問い合わせ", nearby_text="ご連絡はこちらまで",
    )
    assert result.role == ROLE_GENERAL
    assert result.sales_usable == SALES_YES


def test_anchor_text_tier_used_when_no_heading_or_nearby_text():
    result = classify_email_role("x@clinic.jp", anchor_text="ご予約はこちら")
    assert result.role == "RESERVATION"


def test_unknown_preserved_when_all_rich_context_fields_are_insufficient():
    """Context Enrichmentを導入しても、材料が無ければ無理にGENERAL等へ昇格させない。"""
    result = classify_email_role(
        "zx19@clinic.jp", section_heading="", nearby_text="", anchor_text="", parent_text="",
    )
    assert result.role == "UNKNOWN"
    assert result.sales_usable == "REVIEW"


def test_role_confidence_decreases_with_lower_priority_signal():
    """文脈一致(tier1)はlocal-part一致(tier4)より高いrole_confidenceを持つ。"""
    high = classify_email_role("mail@clinic.jp", context_text="お問い合わせはこちら")
    low = classify_email_role("info@clinic.jp")
    assert high.role_confidence > low.role_confidence


_V6_ROLE_FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "email_role_v6_human_audit.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", _V6_ROLE_FIXTURES, ids=lambda c: c["clinic"])
def test_v6_human_audit_role_regression(case):
    result = classify_email_role(
        case["email"], nearby_text=case["nearby_context"], page_url=case["source_url"],
    )
    assert result.role == case["human_role"], case["root_cause"]
