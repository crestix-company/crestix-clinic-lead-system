import csv
from pathlib import Path

import pytest

from src.outreach.article_adapter import ArticleLink, load_article_links_csv, lookup_article
from src.outreach.email_template import DraftValidationError


def record(**changes):
    data = dict(
        clinic_uuid="uuid-001",
        listing_status="LISTED",
        example_article_url="https://clinic.mynavi.jp/article/tokyo_pain-clinic/",
        listing_url="https://clinic.mynavi.jp/article/tokyo_pain-clinic/",
        listing_kind="VERIFIED_ARTICLE",
        source_row="25",
        article_id="article-25",
        verified=True,
    )
    data.update(changes)
    return ArticleLink(**data)


def test_exact_uuid_ready():
    match = lookup_article("UUID-001", [record()])
    assert match.status == "READY"
    assert match.record.article_id == "article-25"


def test_no_mapping():
    assert lookup_article("uuid-002", [record()]).status == "NO_MAPPING"


def test_duplicate_mapping_to_review():
    assert lookup_article("uuid-001", [record(), record(article_id="other")]).status == "NEEDS_REVIEW"


@pytest.mark.parametrize("changes", [
    {"verified": False},
    {"source_row": ""},
    {"listing_status": "NOT_LISTED", "listing_kind": "VERIFIED_PROFILE"},
    {"listing_url": "https://clinic.mynavi.jp/clinic/example/"},
    {"example_article_url": "https://clinic.mynavi.jp.evil.invalid/article/x"},
])
def test_untrusted_match_to_review(changes):
    assert lookup_article("uuid-001", [record(**changes)]).status == "NEEDS_REVIEW"


def test_raw_article_master_rejected(tmp_path: Path):
    path = tmp_path / "article_master.csv"
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["No.", "公開日", "タイトル", "URL", "KW"])
        writer.writeheader()
        writer.writerow({"No.": "1", "公開日": "", "タイトル": "X", "URL": "https://clinic.mynavi.jp/article/x/", "KW": ""})
    with pytest.raises(DraftValidationError):
        load_article_links_csv(path)
