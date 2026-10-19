from datetime import date, datetime

import pytest

from src.outreach.email_template_v2 import (
    ARTICLE_ABSENT,
    ARTICLE_PRESENT,
    ARTICLE_UNVERIFIED,
    ABSENT_PURPOSE,
    LISTED_PURPOSE,
    PHONE_FOLLOWUP_LINE,
    SUBJECT,
    PreviewInput,
    TemplateV2Error,
    _validate_schedule,
    load_settings,
    render_preview,
)
from src.outreach import email_template_v2
from scripts.build_outreach_email_v2_review import approved_v2_body

ARTICLE = "https://clinic.mynavi.jp/article/chofushi_orthodontics/"
DETAIL = "https://clinic.mynavi.jp/clinic/ihff0m1f1d12/"
OTHER_ARTICLE = "https://clinic.mynavi.jp/article/hirakatashi_dental/"
MASTER = frozenset({ARTICLE, OTHER_ARTICLE})


def base(**kwargs):
    values = dict(
        clinic_name="ふじた矯正歯科クリニック",
        director_name="確認済み院長",
        director_name_verified=True,
        article_status=ARTICLE_PRESENT,
        actual_article_url=ARTICLE,
        article_listing_verified=True,
        schedule=("2026-10-19 10:00", "2026-10-20 14:00"),
        phone_call_date=date(2026, 10, 16), email_send_request_confirmed=True,
        recipient_email_verified=True,
        gmail_account_verified=True,
        human_approved=True,
        duplicate_check_complete=True,
        article_master_urls=MASTER,
        as_of=date(2026, 10, 16),
    )
    values.update(kwargs)
    return PreviewInput(**values)


def test_article_present_v2_exact_subject_and_one_actual_url():
    result = render_preview(base())
    assert SUBJECT == "【お伝えしておりましたマイナビ記事について】株式会社Crestixの高原"
    assert result.subject == "【お伝えしておりましたマイナビ記事について】株式会社Crestixの高原"
    assert LISTED_PURPOSE == "現在、マイナビにて掲載させていただいている下記記事について、掲載データの共有・現状のご報告と、掲載内容の確認をさせていただければと考えております。"
    assert result.preview_generated is True
    assert result.ready_for_draft is True
    assert "【掲載いただいている記事】\n" + ARTICLE in result.body
    assert "【記事例】" not in result.body
    assert result.body.count("https://clinic.mynavi.jp/") == 1
    assert LISTED_PURPOSE in result.body
    expected_body = f"""ふじた矯正歯科クリニック
院長 確認済み院長 様

お世話になっております。
株式会社Crestixの高原と申します。

{PHONE_FOLLOWUP_LINE}

{LISTED_PURPOSE}

【掲載いただいている記事】
{ARTICLE}

なお、掲載内容についての行き違い防止のため、お打ち合わせ当日は院長先生にもご参加いただく形でお願いしております。

恐れ入りますが、院長先生がご参加可能な日時を、下記候補よりお知らせいただけますでしょうか。

10月19日（月）10:00
10月20日（火）14:00

上記よりオンラインにて20分～30分ほど頂戴できますと幸いです。
何卒よろしくお願い申し上げます。"""
    assert result.body == expected_body


def test_article_absent_requires_profile_and_absence_evidence_and_uses_two_urls():
    assert ABSENT_PURPOSE == "現在、マイナビクリニックナビにて掲載されている貴院の医院情報について、掲載データの共有・現状のご報告と、掲載内容の確認をさせていただければと考えております。"
    args = base(
        clinic_name="はいはら歯科", article_status=ARTICLE_ABSENT,
        actual_article_url="", article_listing_verified=False,
        recommended_article_url=OTHER_ARTICLE, clinic_detail_url=DETAIL,
        clinic_detail_verified=True, article_absence_verified=True,
    )
    result = render_preview(args)
    assert result.preview_generated is True
    assert "【記事例】\n" + OTHER_ARTICLE in result.body
    assert "【掲載いただいているページ】\n" + DETAIL in result.body
    assert result.body.count("https://clinic.mynavi.jp/") == 2
    assert ABSENT_PURPOSE in result.body
    expected_body = f"""はいはら歯科
院長 確認済み院長 様

お世話になっております。
株式会社Crestixの高原と申します。

{PHONE_FOLLOWUP_LINE}

{ABSENT_PURPOSE}

【記事例】
{OTHER_ARTICLE}

【掲載いただいているページ】
{DETAIL}

なお、掲載内容についての行き違い防止のため、お打ち合わせ当日は院長先生にもご参加いただく形でお願いしております。

恐れ入りますが、院長先生がご参加可能な日時を、下記候補よりお知らせいただけますでしょうか。

10月19日（月）10:00
10月20日（火）14:00

上記よりオンラインにて20分～30分ほど頂戴できますと幸いです。
何卒よろしくお願い申し上げます。"""
    assert result.body == expected_body


def test_present_and_absent_body_differ_only_in_requested_purpose_and_article_block():
    present = render_preview(base())
    absent = render_preview(base(
        article_status=ARTICLE_ABSENT,
        actual_article_url="",
        article_listing_verified=False,
        recommended_article_url=OTHER_ARTICLE,
        clinic_detail_url=DETAIL,
        clinic_detail_verified=True,
        article_absence_verified=True,
    ))
    present_block = f"【掲載いただいている記事】\n{ARTICLE}"
    absent_block = f"【記事例】\n{OTHER_ARTICLE}\n\n【掲載いただいているページ】\n{DETAIL}"
    assert present.body.replace(LISTED_PURPOSE, ABSENT_PURPOSE).replace(present_block, absent_block) == absent.body


def test_article_absent_without_negative_proof_is_blocked():
    result = render_preview(base(article_status=ARTICLE_ABSENT, clinic_detail_verified=True))
    assert result.preview_generated is False
    assert result.body == ""
    assert "紹介記事がないことの確認証拠未確認" in result.blockers


def test_unverified_does_not_emit_any_listed_claim_or_url():
    result = render_preview(base(article_status=ARTICLE_UNVERIFIED, actual_article_url=ARTICLE))
    assert result.preview_generated is False
    assert result.body == ""
    assert "掲載状況未確認" in result.blockers


def test_unconfirmed_phone_keeps_exact_fixed_template_but_blocks_draft():
    result = render_preview(base(phone_call_date=None, email_send_request_confirmed=False))
    assert result.subject == "【お伝えしておりましたマイナビ記事について】株式会社Crestixの高原"
    assert PHONE_FOLLOWUP_LINE in result.body
    assert "株式会社Crestixの高原と申します。" in result.body
    assert result.ready_for_draft is False
    assert "本日架電およびメール送付依頼の確認が必要" in result.blockers


def test_sender_name_uses_exact_kanji_without_automatic_conversion():
    result = render_preview(base())
    assert "株式会社Crestixの高原と申します。" in result.body
    assert "髙原" not in result.subject + result.body
    with pytest.raises(TemplateV2Error, match="送信者名"):
        render_preview(base(sender_name="髙原"))


def test_missing_director_and_schedule_are_preview_only_placeholders():
    result = render_preview(base(director_name="", director_name_verified=False, schedule=()))
    assert "〔院長名確認待ち〕" in result.body
    assert "〔候補日時未設定〕" in result.body
    assert result.preview_generated is True
    assert result.ready_for_draft is False


@pytest.mark.parametrize("url", [
    "https://clinic.mynavi.jp/article/not-in-master/",
    "http://clinic.mynavi.jp/article/x/",
    "https://clinic.mynavi.jp/clinic/x/",
])
def test_article_url_must_be_https_article_and_exact_master_member(url):
    with pytest.raises(TemplateV2Error):
        render_preview(base(actual_article_url=url))


def test_past_or_weekend_dates_are_rejected(monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            fixed = cls(2026, 10, 16, 9, 0)
            return fixed.astimezone(tz) if tz else fixed

    # Deterministic clock for existing schedule validation test, as in PR #98.
    monkeypatch.setattr(email_template_v2, "datetime", FixedDateTime)
    with pytest.raises(TemplateV2Error, match="過去"):
        render_preview(base(schedule=("2026-10-15 10:00",)))
    with pytest.raises(TemplateV2Error, match="営業日"):
        render_preview(base(schedule=("2026-10-18 10:00",)))


def test_same_day_past_time_is_rejected_but_future_time_is_allowed():
    now = datetime(2026, 10, 16, 15, 0)
    with pytest.raises(TemplateV2Error, match="過去"):
        _validate_schedule(("2026-10-16 14:59",), as_of=date(2026, 10, 16), now=now)
    assert _validate_schedule(("2026-10-16 15:01",), as_of=date(2026, 10, 16), now=now)


def test_empty_settings_keep_dates_unset(tmp_path):
    assert load_settings(tmp_path / "missing.json")["candidate_dates"] == []


def test_delivery_body_requires_approved_v2_and_never_uses_legacy_text():
    preview = render_preview(base())
    assert approved_v2_body(preview, human_review_status="未確認") == ""
    assert approved_v2_body(preview, human_review_status="承認") == preview.body

    blocked = render_preview(base(schedule=()))
    assert approved_v2_body(blocked, human_review_status="承認") == ""
