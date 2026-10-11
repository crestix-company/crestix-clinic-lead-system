from types import SimpleNamespace

import pytest

from src.enrichment.email_research import formal_pipeline as module
from src.enrichment.email_research.pipeline import EmailResultRow, PipelineResult


CLINIC = {"clinic_id": 7, "clinic_name": "青空クリニック", "prefecture": "東京都",
          "city": "港区", "address": "東京都港区1-2", "phone": "03-1111-2222", "hp_url": ""}


def verified_row(source="SEARCH_QUERY", query=""):
    return EmailResultRow(
        clinic_id="7", email="info@example.jp", status="VERIFIED_EMAIL", source_type=source,
        source_url="https://example.jp/contact", source_query=query, official_domain="example.jp",
        verified_on_official=True, confidence=1.0, evidence_text="青空クリニック 東京都港区 お問い合わせ",
        checked_at="2026-10-09T00:00:00+00:00", parser_version="test",
    )


class Provider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.queries = []
        self.metrics = {}

    def search(self, query):
        self.queries.append(query)
        return self.responses.pop(0) if self.responses else []


@pytest.fixture
def fake_apply(monkeypatch):
    def apply(*, result, candidates, source_type, source_query_fallback, **kwargs):
        if candidates:
            result.email_rows.append(verified_row(source_type, source_query_fallback))
            return True
        return False
    monkeypatch.setattr(module, "_apply_candidates", apply)


def test_official_true_means_zero_search_calls(monkeypatch):
    result = PipelineResult("7", "青空クリニック", email_rows=[verified_row("OFFICIAL_HP")])
    monkeypatch.setattr(module, "run_pipeline", lambda **kwargs: result)
    provider = Provider([])
    output = module.research_clinic(CLINIC, fetcher=object(), search_provider=provider)
    assert output.research_status == "FOUND"
    assert output.metrics["search_query_count"] == 0
    assert provider.queries == []


def test_navii_true_means_zero_search_calls(monkeypatch, fake_apply):
    monkeypatch.setattr(module, "run_pipeline", lambda **kwargs: PipelineResult("7", "青空クリニック"))
    provider = Provider([])
    output = module.research_clinic(
        CLINIC, fetcher=object(), search_provider=provider,
        trusted_candidates=[{"email": "info@example.jp", "source_url": "https://www.iryou.teikyouseido.mhlw.go.jp/"}],
    )
    assert output.research_status == "FOUND"
    assert output.metrics["search_query_count"] == 0
    assert provider.queries == []


def test_query1_true_skips_query2(monkeypatch, fake_apply):
    monkeypatch.setattr(module, "run_pipeline", lambda **kwargs: PipelineResult("7", "青空クリニック"))
    provider = Provider([[{"url": "https://aozora-clinic.jp", "content": "info@aozora-clinic.jp"}]])
    output = module.research_clinic(CLINIC, fetcher=object(), search_provider=provider)
    assert output.research_status == "FOUND"
    assert provider.queries == ["青空クリニック メールアドレス"]
    assert output.metrics["search_query_count"] == 1


def test_query1_empty_allows_query2_then_no_email_and_never_third(monkeypatch, fake_apply):
    monkeypatch.setattr(module, "run_pipeline", lambda **kwargs: PipelineResult("7", "青空クリニック"))
    provider = Provider([[], [], [{"content": "third@example.jp"}]])
    output = module.research_clinic(CLINIC, fetcher=object(), search_provider=provider)
    assert output.research_status == "NO_EMAIL_FOUND"
    assert provider.queries == [
        "青空クリニック メールアドレス",
        "青空クリニック メールアドレス 東京都 港区",
    ]


def test_auto_false_only_still_runs_remaining_queries_and_finishes_no_email(monkeypatch):
    rejected = EmailResultRow(
        clinic_id="7", email="info@city.example.lg.jp", status="REJECTED_THIRD_PARTY",
        source_type="OFFICIAL_HP", source_url="https://example.jp/contact", source_query="",
        official_domain="example.jp", verified_on_official=False, confidence=0.1,
        evidence_text="", parser_version="test", rationale="government email",
        checked_at="2026-10-09T00:00:00+00:00",
    )
    monkeypatch.setattr(module, "run_pipeline", lambda **kwargs: PipelineResult(
        "7", "青空クリニック", rejected_rows=[rejected]))
    provider = Provider([[], []])
    output = module.research_clinic(CLINIC, fetcher=object(), search_provider=provider)
    assert output.research_status == "NO_EMAIL_FOUND"
    assert len(output.rejection_evidence) == 1
    assert output.metrics["auto_false_count"] == 1
    assert provider.queries == [
        "青空クリニック メールアドレス",
        "青空クリニック メールアドレス 東京都 港区",
    ]
    assert output.metrics["search_query_count"] == 2
