"""Regression tests for the "initial redirect rescue" feature: a clinic's
official-HP URL may redirect to a genuinely new domain (a real site migration)
exactly once, on the PRIMARY fetch only -- never for support-link fetches,
never for crawl(). The destination must still pass is_official_candidate()
(NON_OFFICIAL rejected) and identity() (name/phone/address) before being
accepted; none of SSRF checks, same-domain restriction for crawl, robots.txt,
or the evidence engine/taxonomy/CONFIRMED thresholds are touched.
"""
import pytest

from src.enrichment.hp_analysis import Page
from src.enrichment.safe_web import SafeFetcher, WebError, WebResponse, crawl


def response(text="<html><body>ok</body></html>", status=200, **headers):
    return WebResponse(status, {"content-type": "text/html; charset=utf-8", **headers}, text.encode())


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


class TestSafeFetcherCrossDomainHopBudget:
    def test_same_host_redirect_succeeds_with_default_hop_budget(self):
        transport = SequencedTransport({
            "https://clinic.example/robots.txt": [response("", 404)],
            "https://clinic.example/old": [response("", 301, location="https://clinic.example/new")],
            "https://clinic.example/new": [response("real page")],
        })
        page = _fetcher(transport).fetch("https://clinic.example/old")
        assert page.html == "real page"

    def test_www_variant_redirect_succeeds_with_default_hop_budget(self):
        transport = SequencedTransport({
            "https://clinic.example/robots.txt": [response("", 404)],
            "https://www.clinic.example/robots.txt": [response("", 404)],
            "https://clinic.example/": [response("", 301, location="https://www.clinic.example/")],
            "https://www.clinic.example/": [response("real page")],
        })
        page = _fetcher(transport).fetch("https://clinic.example/")
        assert page.html == "real page"

    def test_cross_domain_redirect_rejected_by_default(self):
        transport = SequencedTransport({
            "https://clinic-old.example/robots.txt": [response("", 404)],
            "https://clinic-old.example/": [response("", 301, location="https://clinic-new.example/")],
        })
        with pytest.raises(WebError, match="別ドメイン"):
            _fetcher(transport).fetch("https://clinic-old.example/")

    def test_cross_domain_redirect_allowed_with_one_hop_budget(self):
        transport = SequencedTransport({
            "https://clinic-old.example/robots.txt": [response("", 404)],
            "https://clinic-new.example/robots.txt": [response("", 404)],
            "https://clinic-old.example/": [response("", 301, location="https://clinic-new.example/")],
            "https://clinic-new.example/": [response("new official page")],
        })
        page = _fetcher(transport).fetch("https://clinic-old.example/", max_cross_domain_hops=1)
        assert page.html == "new official page"
        assert page.url == "https://clinic-new.example/"

    def test_two_cross_domain_redirects_rejected_even_with_one_hop_budget(self):
        transport = SequencedTransport({
            "https://a.example/robots.txt": [response("", 404)],
            "https://b.example/robots.txt": [response("", 404)],
            "https://a.example/": [response("", 301, location="https://b.example/")],
            "https://b.example/": [response("", 301, location="https://c.example/")],
        })
        with pytest.raises(WebError, match="別ドメイン"):
            _fetcher(transport).fetch("https://a.example/", max_cross_domain_hops=1)

    def test_redirect_loop_still_bounded_with_hop_budget(self):
        transport = SequencedTransport({
            "https://clinic.example/robots.txt": [response("", 404)],
            "https://clinic.example/a": [response("", 302, location="https://clinic.example/b")],
            "https://clinic.example/b": [response("", 302, location="https://clinic.example/a")],
        })
        with pytest.raises(WebError, match="リダイレクト回数"):
            _fetcher(transport).fetch("https://clinic.example/a", max_cross_domain_hops=1)

    def test_private_ip_redirect_rejected_even_with_hop_budget(self):
        transport = SequencedTransport({
            "https://clinic.example/robots.txt": [response("", 404)],
            "https://clinic.example/": [response("", 301, location="http://127.0.0.1/")],
        })
        with pytest.raises(WebError):
            _fetcher(transport).fetch("https://clinic.example/", max_cross_domain_hops=1)

    def test_localhost_redirect_rejected_even_with_hop_budget(self):
        transport = SequencedTransport({
            "https://clinic.example/robots.txt": [response("", 404)],
            "https://clinic.example/": [response("", 301, location="http://localhost/")],
        })
        with pytest.raises(WebError):
            _fetcher(transport).fetch("https://clinic.example/", max_cross_domain_hops=1)


class TestCrawlNeverFollowsCrossDomainRegardlessOfHopBudget:
    def test_crawl_cross_domain_links_still_excluded(self):
        first = Page(
            "https://clinic.example/",
            '<a href="/menu/">治療メニュー</a><a href="https://other.example/">別サイト</a>',
        )

        class Fetcher:
            calls = []
            def fetch(self, url, allowed_host=None, max_cross_domain_hops=0):
                self.calls.append(url)
                assert max_cross_domain_hops == 0
                return Page(url, "<p>page</p>")

        f = Fetcher()
        pages, errors = crawl(first, f, max_pages=5)
        assert all("other.example" not in url for url in f.calls)


class TestResearchOneClinicRedirectRescue:
    """Integration-level: research_one_clinic() uses the rescue only on the
    primary fetch, and only accepts the destination after is_official_candidate
    + identity() both pass -- never silently adopting a wrong site."""

    def _record(self, url, clinic_name="医療法人テストクリニック", phone="03-1234-5678", address="東京都千代田区1-1-1"):
        return {
            "clinic_id": 1, "clinic_name": clinic_name, "phone": phone, "address": address,
            "effective_official_hp_url": url, "git_commit_sha": "x", "manifest_id": "m",
            "departments": [],
        }

    def _page_html(self, clinic_name, phone, address):
        return f"<html><head><title>{clinic_name}</title></head><body><h1>{clinic_name}</h1>{address} {phone} 胃カメラ検査を実施しています</body></html>"

    def test_legitimate_cross_domain_redirect_with_identity_match_succeeds(self, monkeypatch):
        from scripts import research_worker as worker

        record = self._record("https://old-domain.example/")
        html = self._page_html(record["clinic_name"], record["phone"], record["address"])
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

    def test_cross_domain_redirect_with_identity_mismatch_does_not_confirm(self):
        from scripts import research_worker as worker

        record = self._record("https://old-domain.example/")
        wrong_clinic_html = self._page_html("全く別の医療法人", "06-9999-9999", "大阪府大阪市2-2-2")
        transport = SequencedTransport({
            "https://old-domain.example/robots.txt": [response("", 404)],
            "https://new-domain.example/robots.txt": [response("", 404)],
            "https://old-domain.example/": [response("", 301, location="https://new-domain.example/")],
            "https://new-domain.example/": [response(wrong_clinic_html)],
        })
        safe_fetcher = SafeFetcher(transport=transport, interval=0)
        fetcher = worker._MeteredFetcher(safe_fetcher)
        rows, fetch_status, candidate_count = worker.research_one_clinic(record, fetcher, {})
        assert fetch_status == "IDENTITY_NOT_VERIFIED"
        assert all(r["research_status"] == "REVIEW" for r in rows)
        assert not any(r["research_status"] == "CONFIRMED" for r in rows)

    def test_redirect_to_external_portal_is_rejected_not_adopted(self):
        from scripts import research_worker as worker

        record = self._record("https://old-domain.example/")
        portal_html = self._page_html(record["clinic_name"], record["phone"], record["address"])
        transport = SequencedTransport({
            "https://old-domain.example/robots.txt": [response("", 404)],
            "https://epark.jp/robots.txt": [response("", 404)],
            "https://old-domain.example/": [response("", 301, location="https://epark.jp/clinics/1")],
            "https://epark.jp/clinics/1": [response(portal_html)],
        })
        safe_fetcher = SafeFetcher(transport=transport, interval=0)
        fetcher = worker._MeteredFetcher(safe_fetcher)
        rows, fetch_status, candidate_count = worker.research_one_clinic(record, fetcher, {})
        assert fetch_status == "FETCH_ERROR"
        assert rows == []  # FETCH_FAILED is clinic-level now; no Treatment rows written

    def test_redirect_to_sns_is_rejected_not_adopted(self):
        from scripts import research_worker as worker

        record = self._record("https://old-domain.example/")
        transport = SequencedTransport({
            "https://old-domain.example/robots.txt": [response("", 404)],
            "https://instagram.com/robots.txt": [response("", 404)],
            "https://old-domain.example/": [response("", 301, location="https://instagram.com/someclinic")],
            "https://instagram.com/someclinic": [response("<html><body>insta</body></html>")],
        })
        safe_fetcher = SafeFetcher(transport=transport, interval=0)
        fetcher = worker._MeteredFetcher(safe_fetcher)
        rows, fetch_status, candidate_count = worker.research_one_clinic(record, fetcher, {})
        assert fetch_status == "FETCH_ERROR"
        assert rows == []  # FETCH_FAILED is clinic-level now; no Treatment rows written
