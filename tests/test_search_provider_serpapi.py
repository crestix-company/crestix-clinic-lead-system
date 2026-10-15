"""SerpApiSearchProvider / EMAIL_SEARCH_PROVIDER切替のunit test。実HTTP接続は行わない。"""
import requests
import pytest

from src.enrichment.search_provider import SearchError, SerpApiSearchProvider, TavilySearchProvider
from src.enrichment.email_research.search_provider_factory import get_email_search_provider


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, payload=None, status_code=200):
        self.payload = payload or {}
        self.status_code = status_code
        self.requests = []

    def get(self, url, params=None, timeout=None):
        self.requests.append({"url": url, "params": params, "timeout": timeout})
        return FakeResponse(self.status_code, self.payload)


def test_explicit_empty_api_key_disables_environment_fallback(monkeypatch):
    monkeypatch.setenv("SERPAPI_API_KEY", "ambient-key-must-not-be-used")
    session = FakeSession()
    provider = SerpApiSearchProvider(api_key="", session=session)
    with pytest.raises(SearchError):
        provider.search("テストクリニック メール")
    assert session.requests == []


def test_search_sends_expected_params():
    session = FakeSession(payload={"organic_results": []})
    provider = SerpApiSearchProvider(api_key="dummy-key", session=session)
    provider.search("テストクリニック メール")
    assert len(session.requests) == 1
    params = session.requests[0]["params"]
    assert params["api_key"] == "dummy-key"
    assert params["engine"] == "google"
    assert params["q"] == "テストクリニック メール"
    assert params["hl"] == "ja"
    assert params["gl"] == "jp"


def test_search_normalizes_organic_results():
    payload = {"organic_results": [
        {"link": "https://clinic.example/contact", "title": "お問い合わせ", "snippet": "info@clinic.example"},
    ]}
    provider = SerpApiSearchProvider(api_key="dummy-key", session=FakeSession(payload=payload))
    results = provider.search("テストクリニック メール")
    assert results == [{"url": "https://clinic.example/contact", "title": "お問い合わせ",
                         "content": "info@clinic.example"}]


def test_search_raises_on_http_error():
    provider = SerpApiSearchProvider(
        api_key="dummy-key", session=FakeSession(status_code=500), backoff_base_seconds=0,
    )
    with pytest.raises(SearchError):
        provider.search("q")


def test_search_raises_on_api_error_field():
    provider = SerpApiSearchProvider(api_key="dummy-key", session=FakeSession(payload={"error": "Invalid API key"}))
    with pytest.raises(SearchError):
        provider.search("q")


def test_search_raw_returns_full_payload_including_ai_overview():
    payload = {"organic_results": [], "ai_overview": {"text_blocks": [{"snippet": "info@clinic.example"}]}}
    provider = SerpApiSearchProvider(api_key="dummy-key", session=FakeSession(payload=payload))
    raw = provider.search_raw("q")
    assert raw["ai_overview"]["text_blocks"][0]["snippet"] == "info@clinic.example"


class RaisingSession:
    """requestsの例外(timeout/connection error)を注入するためのstub。"""
    def __init__(self, exc):
        self.exc = exc
        self.requests = []

    def get(self, url, params=None, timeout=None):
        self.requests.append({"url": url, "params": params, "timeout": timeout})
        raise self.exc


def test_metering_starts_at_zero():
    provider = SerpApiSearchProvider(api_key="dummy-key", session=FakeSession())
    assert all(value == 0 for value in provider.metrics.values())


def test_metering_counts_success():
    provider = SerpApiSearchProvider(api_key="dummy-key", session=FakeSession(payload={"organic_results": []}))
    provider.search("q")
    provider.search("q2")
    assert provider.metrics["attempted"] == 2
    assert provider.metrics["success"] == 2
    assert provider.metrics["failed"] == 0


def test_metering_counts_http_4xx_as_failed():
    provider = SerpApiSearchProvider(api_key="dummy-key", session=FakeSession(status_code=403))
    with pytest.raises(SearchError):
        provider.search("q")
    assert provider.metrics["attempted"] == 1
    assert provider.metrics["http_4xx"] == 1
    assert provider.metrics["http_5xx"] == 0
    assert provider.metrics["failed"] == 1


def test_metering_counts_http_5xx_as_failed():
    provider = SerpApiSearchProvider(
        api_key="dummy-key", session=FakeSession(status_code=503), backoff_base_seconds=0,
    )
    with pytest.raises(SearchError):
        provider.search("q")
    assert provider.metrics["attempted"] == 2
    assert provider.metrics["http_5xx"] == 2
    assert provider.metrics["http_4xx"] == 0
    assert provider.metrics["failed"] == 1


def test_metering_counts_timeout_distinctly_from_other_failures():
    provider = SerpApiSearchProvider(
        api_key="dummy-key", session=RaisingSession(requests.ReadTimeout("slow")),
        backoff_base_seconds=0,
    )
    with pytest.raises(SearchError):
        provider.search("q")
    assert provider.metrics["attempted"] == 3
    assert provider.metrics["timeout"] == 3
    assert provider.metrics["read_timeout"] == 3
    assert provider.metrics["terminal_timeout"] == 1
    assert provider.metrics["retry_count"] == 2
    assert provider.metrics["failed"] == 1
    assert provider.metrics["http_4xx"] == 0 and provider.metrics["http_5xx"] == 0


def test_metering_counts_connection_error_as_failed_not_timeout():
    provider = SerpApiSearchProvider(
        api_key="dummy-key", session=RaisingSession(requests.ConnectionError("down")), backoff_base_seconds=0,
    )
    with pytest.raises(SearchError):
        provider.search("q")
    assert provider.metrics["attempted"] == 3
    assert provider.metrics["failed"] == 1
    assert provider.metrics["timeout"] == 0


class SequenceSession:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def get(self, url, params=None, timeout=None):
        self.requests.append({"url": url, "params": params, "timeout": timeout})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_read_timeout_retries_then_succeeds_with_separate_timeouts():
    session = SequenceSession([
        requests.ReadTimeout("slow"),
        FakeResponse(200, {"organic_results": []}),
    ])
    provider = SerpApiSearchProvider(
        api_key="dummy-key", session=session, connect_timeout=7, read_timeout=45,
        backoff_base_seconds=0,
    )
    assert provider.search("q") == []
    assert [r["timeout"] for r in session.requests] == [(7, 45), (7, 45)]
    assert provider.metrics["query_count"] == 1
    assert provider.metrics["attempted"] == 2
    assert provider.metrics["retry_timeout"] == 1
    assert provider.metrics["success"] == 1
    assert provider.metrics["failed"] == 0


def test_4xx_is_not_retried():
    session = FakeSession(status_code=429)
    provider = SerpApiSearchProvider(api_key="dummy-key", session=session, backoff_base_seconds=0)
    with pytest.raises(SearchError):
        provider.search("q")
    assert len(session.requests) == 1
    assert provider.metrics["retry_count"] == 0


def test_429_with_short_retry_after_is_retried_once():
    first = FakeResponse(status_code=429)
    first.headers = {"Retry-After": "0"}
    second = FakeResponse(status_code=200, payload={"organic_results": []})
    session = SequenceSession([first, second])
    provider = SerpApiSearchProvider(
        api_key="dummy-key", session=session, max_retries=1,
        backoff_base_seconds=0, sleep_fn=lambda _seconds: None,
        retry_rate_limit=True,
    )
    assert provider.search("q") == []
    assert len(session.requests) == 2
    assert provider.metrics["rate_limit"] == 1
    assert provider.metrics["retry_429"] == 1


def test_metering_counts_api_error_field_as_failed():
    provider = SerpApiSearchProvider(api_key="dummy-key", session=FakeSession(payload={"error": "Invalid API key"}))
    with pytest.raises(SearchError):
        provider.search("q")
    assert provider.metrics["attempted"] == 1
    assert provider.metrics["failed"] == 1
    assert provider.metrics["success"] == 0


def test_metering_missing_api_key_does_not_count_as_http_attempt(monkeypatch):
    """APIキー未設定は実際のHTTP試行ではないため、attempted等の集計対象にしない。"""
    monkeypatch.setenv("SERPAPI_API_KEY", "ambient-key-must-not-be-used")
    session = FakeSession()
    provider = SerpApiSearchProvider(api_key="", session=session)
    with pytest.raises(SearchError):
        provider.search("q")
    assert provider.metrics["attempted"] == 0
    assert session.requests == []


def test_none_api_key_uses_environment_configuration(monkeypatch):
    monkeypatch.setenv("SERPAPI_API_KEY", "environment-key")
    session = FakeSession(payload={"organic_results": []})
    provider = SerpApiSearchProvider(api_key=None, session=session)
    assert provider.search("q") == []
    assert session.requests[0]["params"]["api_key"] == "environment-key"


def test_metering_does_not_change_existing_cache_semantics():
    """既存artifact(search_cache.json)の486は「成功response数」であり、メータリング追加後も
    その意味(cache自体はresponse本体のみを保持する)は変更しない。メータリングはproviderインスタンス
    側の実行時カウンタであり、cacheファイルの形式には一切影響しない。"""
    provider = SerpApiSearchProvider(api_key="dummy-key", session=FakeSession(payload={"organic_results": []}))
    result = provider.search("q")
    assert result == []  # search()の戻り値形式は変更していない
    assert provider.metrics["success"] == 1


def test_factory_defaults_to_serpapi(monkeypatch):
    monkeypatch.delenv("EMAIL_SEARCH_PROVIDER", raising=False)
    provider = get_email_search_provider()
    assert isinstance(provider, SerpApiSearchProvider)


def test_factory_env_selects_tavily(monkeypatch):
    monkeypatch.setenv("EMAIL_SEARCH_PROVIDER", "tavily")
    provider = get_email_search_provider()
    assert isinstance(provider, TavilySearchProvider)


def test_factory_explicit_name_overrides_env(monkeypatch):
    monkeypatch.setenv("EMAIL_SEARCH_PROVIDER", "tavily")
    provider = get_email_search_provider("serpapi")
    assert isinstance(provider, SerpApiSearchProvider)


def test_factory_rejects_unknown_provider():
    with pytest.raises(ValueError):
        get_email_search_provider("bing")
