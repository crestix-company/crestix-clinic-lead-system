"""ai_overview_adapter.py のunit test。AI Overviewが無いケースは正常系(ABSENT)であることを重点的に確認する。"""
from src.enrichment.email_research.ai_overview_adapter import (
    AIOverviewAdapter,
    STATUS_ABSENT,
    STATUS_ERROR,
    STATUS_PRESENT,
    extract_candidate_emails_from_ai_overview,
)
from src.enrichment.search_provider import SearchError


class StubProviderNoRawSupport:
    """search_rawを持たないprovider(例: Tavily)。AI OverviewはABSENT扱いになるべき。"""
    def search(self, query):
        return []


class StubProviderWithRaw:
    def __init__(self, payload=None, raise_error=False):
        self.payload = payload or {}
        self.raise_error = raise_error
        self.called_with = []

    def search_raw(self, query):
        self.called_with.append(query)
        if self.raise_error:
            raise SearchError("stub error")
        return self.payload


def test_provider_without_ai_overview_support_is_absent_not_error():
    adapter = AIOverviewAdapter(StubProviderNoRawSupport())
    result = adapter.run("サンプルクリニック メール")
    assert result.status == STATUS_ABSENT
    assert result.candidates == []


def test_missing_ai_overview_key_is_absent():
    adapter = AIOverviewAdapter(StubProviderWithRaw(payload={"organic_results": []}))
    result = adapter.run("q")
    assert result.status == STATUS_ABSENT


def test_empty_ai_overview_dict_is_absent():
    adapter = AIOverviewAdapter(StubProviderWithRaw(payload={"ai_overview": {}}))
    result = adapter.run("q")
    assert result.status == STATUS_ABSENT


def test_provider_error_is_isolated_as_error_status_not_raised():
    adapter = AIOverviewAdapter(StubProviderWithRaw(raise_error=True))
    result = adapter.run("q")  # 例外を投げずにERROR statusを返すこと
    assert result.status == STATUS_ERROR
    assert "stub error" in result.error


def test_present_with_candidate_email_in_text_blocks():
    payload = {"ai_overview": {"text_blocks": [{"snippet": "お問い合わせは info@clinic.example まで"}]}}
    adapter = AIOverviewAdapter(StubProviderWithRaw(payload=payload))
    result = adapter.run("サンプルクリニック メール")
    assert result.status == STATUS_PRESENT
    assert [c.email for c in result.candidates] == ["info@clinic.example"]
    assert result.candidates[0].origin == "AI_OVERVIEW"


def test_present_but_no_literal_email_yields_empty_candidates():
    payload = {"ai_overview": {"text_blocks": [{"snippet": "お問い合わせはフォームからお願いします"}]}}
    adapter = AIOverviewAdapter(StubProviderWithRaw(payload=payload))
    result = adapter.run("q")
    assert result.status == STATUS_PRESENT
    assert result.candidates == []


def test_extract_does_not_guess_generic_addresses():
    payload = {"text_blocks": [{"snippet": "一般的にクリニックはinfo@から始まるメールを使うことが多いです"}]}
    assert extract_candidate_emails_from_ai_overview(payload, "q") == []


def test_extract_flags_third_party_domain():
    payload = {"text_blocks": [{"snippet": "indeed.comの掲載では recruit@indeed.com として連絡可能"}]}
    candidates = extract_candidate_emails_from_ai_overview(payload, "q")
    assert candidates[0].third_party_suspected is True


def test_none_payload_returns_no_candidates():
    assert extract_candidate_emails_from_ai_overview(None, "q") == []
