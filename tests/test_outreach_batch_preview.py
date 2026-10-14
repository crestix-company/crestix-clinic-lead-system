from datetime import date
from src.outreach.article_adapter import ArticleLink
from src.outreach.batch_preview import ClinicContact, preview_batch
from src.outreach.email_template import five_weekdays


def generate(contacts, verified=True):
    links = [ArticleLink(
        clinic_uuid="uuid-001", listing_status="LISTED",
        example_article_url="https://clinic.mynavi.jp/article/tokyo_pain-clinic/",
        listing_url="https://clinic.mynavi.jp/article/tokyo_pain-clinic/",
        listing_kind="VERIFIED_ARTICLE", source_row="1", article_id="1", verified=verified,
    )]
    return preview_batch(
        contacts, links, is_staff_name="前田",
        candidate_dates=five_weekdays(date(2026, 10, 12)),
        called_today_and_told_to_email=True,
        director_attendance_requested=True,
    )


def contact(**changes):
    values = dict(clinic_uuid="uuid-001", clinic_name="テスト医院",
                  primary_email="clinic@example.jp", recipient_role="院長",
                  recipient_name="田中 太郎", email_verified=True)
    values.update(changes)
    return ClinicContact(**values)


def test_valid_batch_preview():
    result = generate([contact()])[0]
    assert result.status == "READY"
    assert result.to == "clinic@example.jp"
    assert "株式会社Crestixの前田" in result.body


def test_unverified_contact_never_ready():
    assert generate([contact(email_verified=False)])[0].status == "NEEDS_REVIEW"


def test_duplicate_clinic_uuid_never_ready():
    assert all(row.status == "NEEDS_REVIEW" for row in generate([contact(), contact()]))


def test_missing_article_join_never_ready():
    assert generate([contact(clinic_uuid="uuid-other")])[0].status == "NO_MAPPING"


def test_unverified_article_never_ready():
    assert generate([contact()], verified=False)[0].status == "NEEDS_REVIEW"
