"""Regression tests for the robots.txt-only cross-authority redirect policy
(RFC 9309 §2.3.1.2): robots.txt may redirect across hosts up to
MAX_ROBOTS_REDIRECT_HOPS times, while the page fetch itself, crawl(), and the
initial-fetch redirect rescue (commit 91144cf) are all completely unaffected.
Every hop still goes through validate_url() (SSRF/public-IP/port/userinfo);
none of that is weakened.
"""
import pytest

from src.enrichment.hp_analysis import Page
from src.enrichment.safe_web import MAX_ROBOTS_REDIRECT_HOPS, SafeFetcher, WebError, WebResponse, crawl


def response(text="<html><body>ok</body></html>", status=200, **headers):
    return WebResponse(status, {"content-type": "text/html; charset=utf-8", **headers}, text.encode())


def robots(text="User-agent: *\nDisallow:\n", status=200, **headers):
    return WebResponse(status, {"content-type": "text/plain", **headers}, text.encode())


class SequencedTransport:
    def __init__(self, routes):
        self.routes = {url: list(items) for url, items in routes.items()}
        self.calls = []

    def get(self, url, timeout, max_bytes):
        self.calls.append(url)
        queue = self.routes[url]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item


def _fetcher(transport):
    return SafeFetcher(transport, interval=0)


class TestRobotsSameHostRedirect:
    def test_robots_same_host_redirect_rules_applied(self):
        transport = SequencedTransport({
            "https://clinic.example/robots.txt": [response("", 301, location="https://clinic.example/robots-new.txt")],
            "https://clinic.example/robots-new.txt": [robots("User-agent: *\nDisallow: /private/\n")],
            "https://clinic.example/": [response("page")],
            "https://clinic.example/private/": [response("secret")],
        })
        fetcher = _fetcher(transport)
        page = fetcher.fetch("https://clinic.example/")
        assert page.html == "page"
        with pytest.raises(WebError, match="許可されていません"):
            fetcher.fetch("https://clinic.example/private/")


class TestRobotsCrossHostRedirect:
    def test_robots_cross_host_1hop_rules_apply_to_initial_authority(self):
        transport = SequencedTransport({
            "https://clinic.example/robots.txt": [response("", 301, location="https://cdn.other.example/robots.txt")],
            "https://cdn.other.example/robots.txt": [robots("User-agent: *\nDisallow: /blocked/\n")],
            "https://clinic.example/": [response("page")],
            "https://clinic.example/blocked/": [response("secret")],
        })
        fetcher = _fetcher(transport)
        page = fetcher.fetch("https://clinic.example/")
        assert page.html == "page"
        # Rule from the cross-host robots.txt is applied under clinic.example
        # (the ORIGINAL authority), not under cdn.other.example.
        with pytest.raises(WebError, match="許可されていません"):
            fetcher.fetch("https://clinic.example/blocked/")

    def test_robots_cross_host_exactly_5_hops_succeeds(self):
        routes = {"https://clinic.example/": [response("page")]}
        current = "https://clinic.example/robots.txt"
        for i in range(MAX_ROBOTS_REDIRECT_HOPS):
            nxt = f"https://hop{i}.example/robots.txt"
            routes[current] = [response("", 301, location=nxt)]
            current = nxt
        routes[current] = [robots()]
        transport = SequencedTransport(routes)
        page = _fetcher(transport).fetch("https://clinic.example/")
        assert page.html == "page"

    def test_robots_6_hops_fails_closed(self):
        routes = {"https://clinic.example/": [response("page")]}
        current = "https://clinic.example/robots.txt"
        for i in range(MAX_ROBOTS_REDIRECT_HOPS + 1):
            nxt = f"https://hop{i}.example/robots.txt"
            routes[current] = [response("", 301, location=nxt)]
            current = nxt
        routes[current] = [robots()]
        transport = SequencedTransport(routes)
        with pytest.raises(WebError, match="見送り"):
            _fetcher(transport).fetch("https://clinic.example/")

    def test_robots_redirect_loop_fails_closed(self):
        transport = SequencedTransport({
            "https://clinic.example/robots.txt": [response("", 301, location="https://a.example/robots.txt")],
            "https://a.example/robots.txt": [response("", 301, location="https://b.example/robots.txt")],
            "https://b.example/robots.txt": [response("", 301, location="https://a.example/robots.txt")],
            "https://clinic.example/": [response("page")],
        })
        with pytest.raises(WebError, match="見送り"):
            _fetcher(transport).fetch("https://clinic.example/")


class TestRobotsCrossHostUnsafeDestination:
    def test_robots_redirect_to_private_ip_rejected(self):
        transport = SequencedTransport({
            "https://clinic.example/robots.txt": [response("", 301, location="http://10.0.0.5/robots.txt")],
            "https://clinic.example/": [response("page")],
        })
        with pytest.raises(WebError, match="見送り"):
            _fetcher(transport).fetch("https://clinic.example/")

    def test_robots_redirect_to_localhost_rejected(self):
        transport = SequencedTransport({
            "https://clinic.example/robots.txt": [response("", 301, location="http://localhost/robots.txt")],
            "https://clinic.example/": [response("page")],
        })
        with pytest.raises(WebError, match="見送り"):
            _fetcher(transport).fetch("https://clinic.example/")


class TestRobotsFinalStatusHandling:
    def test_robots_final_404_treated_as_unavailable_allow_all(self):
        transport = SequencedTransport({
            "https://clinic.example/robots.txt": [response("", 301, location="https://other.example/robots.txt")],
            "https://other.example/robots.txt": [response("", 404)],
            "https://clinic.example/": [response("page")],
        })
        page = _fetcher(transport).fetch("https://clinic.example/")
        assert page.html == "page"

    def test_robots_final_503_fails_closed(self):
        transport = SequencedTransport({
            "https://clinic.example/robots.txt": [response("", 301, location="https://other.example/robots.txt")],
            "https://other.example/robots.txt": [response("", 503)],
            "https://clinic.example/": [response("page")],
        })
        with pytest.raises(WebError, match="見送り"):
            _fetcher(transport).fetch("https://clinic.example/")

    def test_robots_final_429_fails_closed_not_treated_like_404(self):
        transport = SequencedTransport({
            "https://clinic.example/robots.txt": [response("", 429)],
            "https://clinic.example/": [response("page")],
        })
        with pytest.raises(WebError, match="見送り"):
            _fetcher(transport).fetch("https://clinic.example/")

    def test_robots_final_html_error_page_fails_closed(self):
        # Real production case: a clinic's robots.txt redirects cross-host to a
        # completely different, unrelated business's live 200 OK homepage.
        # This must NOT be silently treated as "robots unavailable".
        other_site_html = "<html><head><title>別の医院のホームページ</title></head><body>いらっしゃいませ</body></html>"
        transport = SequencedTransport({
            "https://clinic.example/robots.txt": [response("", 302, location="https://unrelated-business.example/error.html")],
            "https://unrelated-business.example/error.html": [response(other_site_html, 200)],
            "https://clinic.example/": [response("page")],
        })
        with pytest.raises(WebError, match="見送り"):
            _fetcher(transport).fetch("https://clinic.example/")


class TestNormalPagePolicyUnchanged:
    def test_normal_page_cross_domain_redirect_still_blocked_by_default(self):
        transport = SequencedTransport({
            "https://clinic-old.example/robots.txt": [response("", 404)],
            "https://clinic-old.example/": [response("", 301, location="https://clinic-new.example/")],
        })
        with pytest.raises(WebError, match="別ドメイン"):
            _fetcher(transport).fetch("https://clinic-old.example/")


class TestCrawlStillNeverFollowsCrossDomain:
    def test_crawl_cross_domain_links_still_excluded(self):
        first = Page(
            "https://clinic.example/",
            '<a href="/menu/">治療メニュー</a><a href="https://other.example/">別サイト</a>',
        )

        class Fetcher:
            def fetch(self, url, allowed_host=None, max_cross_domain_hops=0):
                assert max_cross_domain_hops == 0
                return Page(url, "<p>page</p>")

        pages, errors = crawl(first, Fetcher(), max_pages=5)
        assert all("other.example" not in p.url for p in pages)


class TestInitialRedirectRescueUnaffected:
    def test_initial_fetch_identity_verification_path_unaffected(self):
        from scripts import research_worker as worker

        record = {
            "clinic_id": 1, "clinic_name": "医療法人テストクリニック", "phone": "03-1234-5678",
            "address": "東京都千代田区1-1-1", "effective_official_hp_url": "https://old-domain.example/",
            "git_commit_sha": "x", "manifest_id": "m", "departments": [],
        }
        html = ("<html><head><title>医療法人テストクリニック</title></head><body>"
                "<h1>医療法人テストクリニック</h1>東京都千代田区1-1-1 03-1234-5678 "
                "胃カメラ検査を実施しています</body></html>")
        transport = SequencedTransport({
            "https://old-domain.example/robots.txt": [response("", 404)],
            "https://new-domain.example/robots.txt": [response("", 404)],
            "https://old-domain.example/": [response("", 301, location="https://new-domain.example/")],
            "https://new-domain.example/": [response(html)],
        })
        safe_fetcher = SafeFetcher(transport=transport, interval=0)
        fetcher = worker._MeteredFetcher(safe_fetcher)
        rows, fetch_status, candidate_count = worker.research_one_clinic(record, fetcher, {})
        assert fetch_status == "OK"
        assert any(r["research_status"] == "CONFIRMED" for r in rows)
