from datetime import date
import pytest

from src.outreach.email_template import DraftInput, DraftValidationError, five_weekdays, render_draft


def sample(**overrides):
    payload = dict(
        clinic_name="州会クリニック",
        recipient_role="院長",
        recipient_name="永井 一成",
        is_staff_name="前田",
        example_article_url="https://clinic.mynavi.jp/article/tokyo_pain-clinic/",
        listing_url="https://clinic.mynavi.jp/article/tokyo_pain-clinic/",
        listing_kind="VERIFIED_ARTICLE",
        listing_verified=True,
        called_today_and_told_to_email=True,
        director_attendance_requested=True,
        candidate_dates=five_weekdays(date(2026, 10, 12)),
    )
    payload.update(overrides)
    return DraftInput(**payload)


def test_template_snapshot():
    draft = render_draft(sample())
    assert draft.subject == "【お伝えしておりましたマイナビ記事について】株式会社Crestix 前田"
    assert "院長 永井 一成 様" in draft.body
    assert "10月12日（月）9:00～21:00" in draft.body
    assert "10月16日（金）9:00～21:00" in draft.body
    assert "******************************************" in draft.body
    assert draft.body.count("******************************************") == 2


@pytest.mark.parametrize("changes", [
    {"recipient_name": ""},
    {"listing_verified": False},
    {"listing_kind": "VERIFIED_PROFILE"},
    {"called_today_and_told_to_email": False},
    {"director_attendance_requested": False},
    {"example_article_url": "https://malicious.example/article/foo"},
    {"candidate_dates": (date(2026, 10, 12),)},
])
def test_requires_review(changes):
    with pytest.raises(DraftValidationError):
        render_draft(sample(**changes))


def test_skip_weekend():
    assert five_weekdays(date(2026, 10, 16)) == (
        date(2026, 10, 16), date(2026, 10, 19), date(2026, 10, 20),
        date(2026, 10, 21), date(2026, 10, 22),
    )
