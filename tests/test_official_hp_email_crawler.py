"""official_hp_email_crawler.pyのunit test。実HTTP接続は行わず、SafeFetcherへstub transportを注入する。"""
from urllib.parse import urlsplit

import pytest

from src.enrichment.safe_web import SafeFetcher, WebResponse
from src.enrichment.email_research.official_hp_email_crawler import crawl_clinic, MAX_DEPTH, MAX_PAGES


class StubTransport:
    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.requested = []

    def get(self, url, timeout, max_bytes):
        self.requested.append(url)
        path = urlsplit(url).path or "/"
        key = f"https://{urlsplit(url).hostname}{path}"
        if key in self.pages:
            return WebResponse(200, {"content-type": "text/html; charset=utf-8"}, self.pages[key].encode())
        return WebResponse(404, {"content-type": "text/html"}, b"<html>not found</html>")


class AllowAllFetcher(SafeFetcher):
    """robots.txt取得自体はこのテストの対象外のため、常に許可する軽量サブクラス。"""
    def allowed(self, url):
        return True


@pytest.fixture
def fetcher_factory():
    def _make(pages):
        return AllowAllFetcher(transport=StubTransport(pages), interval=0)
    return _make


def test_mailto_found_on_contact_page(fetcher_factory):
    pages = {
        "https://clinic.test/": '<html><title>Top</title><body><a href="/contact">Contact</a></body></html>',
        "https://clinic.test/contact": '<html><title>Contact</title><body>'
                                        '<a href="mailto:info@clinic.test">Mail</a></body></html>',
    }
    result = crawl_clinic("1", "テストクリニック", "https://clinic.test/", fetcher=fetcher_factory(pages))
    assert result.research_status == "FOUND"
    assert [e.email for e in result.emails] == ["info@clinic.test"]
    assert result.emails[0].extraction_method == "MAILTO"


def test_form_only_when_no_email_but_form_present(fetcher_factory):
    pages = {
        "https://clinic.test/": '<html><title>Top</title><body><a href="/contact">Contact</a></body></html>',
        "https://clinic.test/contact": '<html><title>お問い合わせ</title><body>'
                                        '<h2>お問い合わせ</h2>'
                                        '<form action="/submit"><input name="name"></form></body></html>',
    }
    result = crawl_clinic("1", "テストクリニック", "https://clinic.test/", fetcher=fetcher_factory(pages))
    assert result.research_status == "FORM_ONLY"
    assert result.forms[0].form_type == "GENERAL"


def test_no_email_found_when_nothing_present(fetcher_factory):
    pages = {"https://clinic.test/": '<html><title>Top</title><body>ご案内のみ</body></html>'}
    result = crawl_clinic("1", "テストクリニック", "https://clinic.test/", fetcher=fetcher_factory(pages))
    assert result.research_status == "NO_EMAIL_FOUND"
    assert result.emails == []
    assert result.forms == []


def test_fetch_failed_when_top_page_unreachable(fetcher_factory):
    result = crawl_clinic("1", "テストクリニック", "https://clinic.test/", fetcher=fetcher_factory({}))
    assert result.research_status == "FETCH_FAILED"
    assert result.pages[0].status == "FETCH_FAILED"


def test_off_domain_links_are_never_queued(fetcher_factory):
    pages = {
        "https://clinic.test/": (
            '<html><title>Top</title><body>'
            '<a href="https://other-clinic.test/contact">他院へのリンク</a>'
            '<a href="/access">アクセス</a>'
            '</body></html>'
        ),
        "https://clinic.test/access": '<html><title>Access</title><body>当院の地図</body></html>',
    }
    fetcher = fetcher_factory(pages)
    result = crawl_clinic("1", "テストクリニック", "https://clinic.test/", fetcher=fetcher)
    requested_hosts = {urlsplit(u).hostname for u in fetcher.transport.requested}
    assert requested_hosts == {"clinic.test"}  # other-clinic.testへは一切アクセスしない


def test_max_pages_limit_respected(fetcher_factory):
    # トップ + 候補リンク5件(合計6ページ上限)を用意し、7件目以降が取得されないことを確認する。
    links = "".join(f'<a href="/page{i}">page{i}</a>' for i in range(10))
    pages = {"https://clinic.test/": f'<html><title>Top</title><body>{links}</body></html>'}
    for i in range(10):
        pages[f"https://clinic.test/page{i}"] = f'<html><title>Page {i}</title><body>text</body></html>'
    result = crawl_clinic("1", "テストクリニック", "https://clinic.test/", fetcher=fetcher_factory(pages))
    fetched = [p for p in result.pages if p.status == "FETCHED"]
    assert len(fetched) <= MAX_PAGES


def test_depth_beyond_max_depth_not_followed(fetcher_factory):
    # depth0(トップ)->depth1(/a)->depth2(/b)までは辿るが、depth2のページから新たに
    # リンクされるdepth3相当のページは辿らない。
    pages = {
        "https://clinic.test/": '<html><title>Top</title><body><a href="/a">a</a></body></html>',
        "https://clinic.test/a": '<html><title>A</title><body><a href="/b">b</a></body></html>',
        "https://clinic.test/b": '<html><title>B</title><body><a href="/c">c (too deep)</a></body></html>',
        "https://clinic.test/c": '<html><title>C</title><body>should not be fetched</body></html>',
    }
    result = crawl_clinic("1", "テストクリニック", "https://clinic.test/", fetcher=fetcher_factory(pages), max_pages=10)
    fetched_urls = {p.final_url for p in result.pages if p.status == "FETCHED"}
    assert "https://clinic.test/c" not in fetched_urls
    assert max(p.depth for p in result.pages) <= MAX_DEPTH


@pytest.mark.parametrize("path", ["/blog/", "/news/post-1", "/column/doctor", "/archive/2025/"])
def test_article_pages_do_not_contribute_email_even_when_mailto(path, fetcher_factory):
    pages = {
        "https://clinic.test/": f'<html><body><a href="{path}">記事</a></body></html>',
        f"https://clinic.test{path}": (
            '<html><title>医院ブログ</title><body><article>'
            '<a href="mailto:writer@clinic.test">執筆者メール</a>'
            '</article></body></html>'
        ),
    }
    result = crawl_clinic("1", "テストクリニック", "https://clinic.test/", fetcher=fetcher_factory(pages))
    assert result.emails == []
    assert result.research_status == "NO_EMAIL_FOUND"


def test_third_party_email_is_not_adopted_from_blog(fetcher_factory):
    pages = {
        "https://clinic.test/": '<html><body><a href="/blog/">Blog</a></body></html>',
        "https://clinic.test/blog/": (
            '<html><body><a href="mailto:cyberpsy@provider.example.jp">メール</a>'
            '<p>mhb@yahoo.ne.jp</p></body></html>'
        ),
    }
    result = crawl_clinic("1", "テストクリニック", "https://clinic.test/", fetcher=fetcher_factory(pages))
    assert result.emails == []


def test_third_party_domain_is_not_adopted_on_contact_page(fetcher_factory):
    pages = {
        "https://clinic.test/": '<html><body><a href="/contact/">お問い合わせ</a></body></html>',
        "https://clinic.test/contact/": (
            '<html><title>お問い合わせ</title><body>'
            '<a href="mailto:vendor@form-service.example">vendor@form-service.example</a>'
            '<a href="mailto:info@clinic.test">info@clinic.test</a>'
            '</body></html>'
        ),
    }
    result = crawl_clinic("1", "テストクリニック", "https://clinic.test/", fetcher=fetcher_factory(pages))
    assert [email.email for email in result.emails] == ["info@clinic.test"]


def test_mailto_href_disagreeing_with_displayed_email_is_rejected(fetcher_factory):
    pages = {
        "https://clinic.test/": (
            '<html><title>医院概要</title><body>'
            '<a href="mailto:person@clinic.test">contact@clinic.test</a>'
            '</body></html>'
        ),
    }
    result = crawl_clinic("1", "テストクリニック", "https://clinic.test/", fetcher=fetcher_factory(pages))
    # PAGE_TEXTとして明示された表示アドレスだけが残り、矛盾したmailto hrefは採用しない。
    assert [email.email for email in result.emails] == ["contact@clinic.test"]


def test_doctor_biography_title_does_not_contribute_email(fetcher_factory):
    pages = {
        "https://clinic.test/": '<html><body><a href="/doctor">医師紹介</a></body></html>',
        "https://clinic.test/doctor": (
            '<html><title>院長経歴</title><body>以前の勤務先: doctor@clinic.test</body></html>'
        ),
    }
    result = crawl_clinic("1", "テストクリニック", "https://clinic.test/", fetcher=fetcher_factory(pages))
    assert result.emails == []


def test_embedded_article_body_email_is_ignored_but_footer_email_is_kept(fetcher_factory):
    pages = {
        "https://clinic.test/": (
            '<html><body><article><p>寄稿者 writer@clinic.test</p></article>'
            '<footer><a href="mailto:info@clinic.test">お問い合わせ</a></footer></body></html>'
        ),
    }
    result = crawl_clinic("1", "テストクリニック", "https://clinic.test/", fetcher=fetcher_factory(pages))
    assert [email.email for email in result.emails] == ["info@clinic.test"]


def test_article_links_are_not_fetched(fetcher_factory):
    pages = {
        "https://clinic.test/": (
            '<html><body><a href="/blog/">ブログ</a>'
            '<a href="/contact/">お問い合わせ</a></body></html>'
        ),
        "https://clinic.test/blog/": '<html><body>writer@clinic.test</body></html>',
        "https://clinic.test/contact/": '<html><body>info@clinic.test</body></html>',
    }
    fetcher = fetcher_factory(pages)
    result = crawl_clinic("1", "テストクリニック", "https://clinic.test/", fetcher=fetcher)
    requested_paths = {urlsplit(url).path for url in fetcher.transport.requested}
    assert "/blog/" not in requested_paths
    assert [email.email for email in result.emails] == ["info@clinic.test"]
