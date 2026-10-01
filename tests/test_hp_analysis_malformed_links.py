"""Regression tests for malformed hrefs encountered during Treatment Research."""
import pytest

from src.enrichment.hp_analysis import Page, canonical_page, is_official_candidate


BROKEN_CITATION = (
    "A Single-Blinded Prospective Study on Using Botulinum Toxin Type A "
    "for Reducing Alar Mobility ：Yehong Zhong, Dejun Cao,et al"
)


def test_page_skips_malformed_protocol_relative_citation():
    page = Page(
        "https://example-clinic.jp/",
        f'<a href="//{BROKEN_CITATION}">reference</a><p>body</p>',
    )
    assert page.links == []


def test_page_skips_malformed_fullwidth_hash_netloc():
    page = Page(
        "https://example-clinic.jp/",
        '<a href="//＃">share</a><p>body</p>',
    )
    assert page.links == []


def test_page_keeps_valid_relative_http_link():
    page = Page(
        "https://example-clinic.jp/base/",
        '<a href="/treatment/endoscopy">treatment</a>',
    )
    assert page.links == [
        {"url": "https://example-clinic.jp/treatment/endoscopy", "text": "treatment"}
    ]


@pytest.mark.parametrize(
    "href",
    [
        "mailto:info@example-clinic.jp",
        "tel:0312345678",
        "javascript:void(0)",
        "data:text/plain,hello",
        "#section",
    ],
)
def test_page_skips_non_fetchable_links(href):
    page = Page(
        "https://example-clinic.jp/",
        f'<a href="{href}">skip</a>',
    )
    assert page.links == []


def test_canonical_page_handles_malformed_citation_without_raising():
    assert canonical_page(f"https://{BROKEN_CITATION}/") == ""


def test_canonical_page_handles_fullwidth_hash_netloc_without_raising():
    assert canonical_page("https://＃/") == ""


def test_is_official_candidate_handles_malformed_netloc_without_raising():
    assert is_official_candidate(f"https://{BROKEN_CITATION}/") is False
    assert is_official_candidate("https://＃/") is False
