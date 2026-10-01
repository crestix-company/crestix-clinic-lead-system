"""Step 4 regression tests for the 429/Retry-After bounded single retry added to
SafeFetcher._get() (src/enrichment/safe_web.py). Scope is strictly "transient
fetch retry" -- no change to SSRF checks, public-IP validation, same-domain
restriction, robots.txt enforcement, User-Agent, or max HTML size, and none of
these tests touch the evidence engine/taxonomy/CONFIRMED thresholds.
"""
import pytest

from src.enrichment.safe_web import SafeFetcher, WebError, WebResponse


class SequencedTransport:
    """Each url maps to a list of responses/exceptions, consumed in order --
    lets a stub simulate "429 once, then 200" for the same URL."""
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


def response(text="<html><body>ok</body></html>", status=200, **headers):
    return WebResponse(status, {"content-type": "text/html; charset=utf-8", **headers}, text.encode())


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def _fetcher(sleeps):
    def make(transport):
        return SafeFetcher(transport, interval=0, sleeper=lambda s: sleeps.append(s))
    return make


class Test429RetryAfterBoundedRetry:
    def test_429_with_short_retry_after_retries_once_then_succeeds(self, _fetcher, sleeps):
        transport = SequencedTransport({
            "https://clinic.example/robots.txt": [response("", 404)],
            "https://clinic.example/": [response("", 429, **{"retry-after": "2"}), response("page ok")],
        })
        page = _fetcher(transport).fetch("https://clinic.example/")
        assert page.html == "page ok"
        assert sleeps == [2]
        assert transport.calls.count("https://clinic.example/") == 2

    def test_429_without_retry_after_is_not_retried(self, _fetcher, sleeps):
        transport = SequencedTransport({
            "https://clinic.example/robots.txt": [response("", 404)],
            "https://clinic.example/": [response("", 429)],
        })
        fetcher = _fetcher(transport)
        try:
            fetcher.fetch("https://clinic.example/")
            assert False, "expected WebError"
        except WebError as exc:
            assert "429" in str(exc)
        assert transport.calls.count("https://clinic.example/") == 1
        assert sleeps == []

    def test_429_with_excessive_retry_after_is_not_retried(self, _fetcher, sleeps):
        from src.enrichment.safe_web import MAX_RETRY_AFTER_SECONDS
        transport = SequencedTransport({
            "https://clinic.example/robots.txt": [response("", 404)],
            "https://clinic.example/": [response("", 429, **{"retry-after": str(MAX_RETRY_AFTER_SECONDS + 1)})],
        })
        fetcher = _fetcher(transport)
        try:
            fetcher.fetch("https://clinic.example/")
            assert False, "expected WebError"
        except WebError as exc:
            assert "429" in str(exc)
        assert transport.calls.count("https://clinic.example/") == 1
        assert sleeps == []

    def test_429_retry_is_bounded_to_one_even_if_still_429_after_retry(self, _fetcher, sleeps):
        # queue has only two entries but SequencedTransport repeats the last one,
        # so a buggy unbounded-retry implementation would loop forever here.
        transport = SequencedTransport({
            "https://clinic.example/robots.txt": [response("", 404)],
            "https://clinic.example/": [
                response("", 429, **{"retry-after": "1"}),
                response("", 429, **{"retry-after": "1"}),
            ],
        })
        fetcher = _fetcher(transport)
        try:
            fetcher.fetch("https://clinic.example/")
            assert False, "expected WebError"
        except WebError as exc:
            assert "429" in str(exc)
        # exactly one retry: 2 attempts at "/" (first 429, retried once, still 429 -> raise)
        assert transport.calls.count("https://clinic.example/") == 2
        assert sleeps == [1]

    def test_429_non_numeric_retry_after_is_not_retried(self, _fetcher, sleeps):
        transport = SequencedTransport({
            "https://clinic.example/robots.txt": [response("", 404)],
            "https://clinic.example/": [response("", 429, **{"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"})],
        })
        fetcher = _fetcher(transport)
        try:
            fetcher.fetch("https://clinic.example/")
            assert False, "expected WebError"
        except WebError as exc:
            assert "429" in str(exc)
        assert transport.calls.count("https://clinic.example/") == 1
