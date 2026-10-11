"""search_candidate_adapter.py のunit test。実HTTP接続は行わない(providerはstub)。"""
from src.enrichment.email_research.search_candidate_adapter import (
    SearchCandidateAdapter,
    build_queries,
    extract_candidate_emails_from_results,
    is_third_party_domain,
)
from src.enrichment.search_provider import SearchError


class StubProvider:
    def __init__(self, by_query=None, raise_on=None):
        self.by_query = by_query or {}
        self.raise_on = raise_on or set()
        self.called_with = []

    def search(self, query):
        self.called_with.append(query)
        if query in self.raise_on:
            raise SearchError("stub error")
        return self.by_query.get(query, [])


def test_build_queries_plain_templates_only_without_location():
    queries = build_queries("サンプルクリニック")
    assert queries == ["サンプルクリニック メール", "サンプルクリニック メールアドレス", "サンプルクリニック お問い合わせ"]


def test_build_queries_adds_disambiguation_with_prefecture_and_city():
    queries = build_queries("サンプルクリニック", prefecture="東京都", city="渋谷区")
    assert "サンプルクリニック メール" in queries
    assert "サンプルクリニック 東京都渋谷区 メール" in queries
    assert "サンプルクリニック 東京都渋谷区 メールアドレス" in queries
    assert "サンプルクリニック 東京都渋谷区 お問い合わせ" in queries


def test_build_queries_dedupes_and_respects_flag():
    queries = build_queries("サンプルクリニック", prefecture="東京都", city="渋谷区", include_disambiguation=False)
    assert queries == ["サンプルクリニック メール", "サンプルクリニック メールアドレス", "サンプルクリニック お問い合わせ"]


def test_build_queries_empty_name_returns_empty():
    assert build_queries("") == []


def test_extract_candidate_emails_literal_only_no_guessing():
    results = [{"url": "https://clinic.example/contact", "title": "お問い合わせ", "content": "特にメール記載なし"}]
    candidates = extract_candidate_emails_from_results(results, "サンプルクリニック メール")
    assert candidates == []  # 記載が無い限り候補を合成しない(info@等の推測禁止)


def test_extract_candidate_emails_from_snippet():
    results = [{"url": "https://clinic.example/contact", "title": "お問い合わせ",
                "content": "ご連絡は info@clinic.example まで"}]
    candidates = extract_candidate_emails_from_results(results, "q")
    assert [c.email for c in candidates] == ["info@clinic.example"]
    assert candidates[0].field_matched == "SNIPPET"


def test_extract_candidate_emails_dedupes_across_fields():
    results = [{"url": "https://clinic.example/contact", "title": "info@clinic.example",
                "content": "info@clinic.example"}]
    candidates = extract_candidate_emails_from_results(results, "q")
    assert len(candidates) == 1


def test_extract_candidate_rejects_dummy_addresses():
    results = [{"url": "https://clinic.example", "title": "", "content": "test@example.com にご連絡ください"}]
    assert extract_candidate_emails_from_results(results, "q") == []


def test_third_party_domain_flagged_from_known_blocklist():
    results = [{"url": "https://job-medley.com/clinic/sample", "title": "求人",
                "content": "recruit@job-medley.com"}]
    candidates = extract_candidate_emails_from_results(results, "q")
    assert candidates[0].third_party_suspected is True
    assert candidates[0].source_owner_type == "THIRD_PARTY_PORTAL"
    assert candidates[0].email_ownership == "OWNERSHIP_UNKNOWN"


def test_mhlw_page_owner_does_not_become_email_owner_at_discovery_time():
    candidates = extract_candidate_emails_from_results([{
        "url": "https://www.iryou.teikyouseido.mhlw.go.jp/znk-web/juminkanja/S2430/initialize?kikanCd=123&kikanKbn=2&prefCd=13",
        "title": "サンプルクリニック", "content": "info@clinic.example",
    }], "サンプルクリニック メールアドレス")
    assert len(candidates) == 1
    assert candidates[0].source_owner_type == "TRUSTED_PUBLIC_DIRECTORY"
    assert candidates[0].email_ownership == "OWNERSHIP_UNKNOWN"
    assert candidates[0].third_party_suspected is False


def test_is_third_party_domain_reuses_hp_analysis_non_official():
    assert is_third_party_domain("https://www.instagram.com/sample_clinic/") is True
    assert is_third_party_domain("https://clinic.example/contact") is False


def test_is_third_party_domain_extra_job_portal_list():
    assert is_third_party_domain("indeed.com") is True
    assert is_third_party_domain("https://www.wixsite.com/sample-clinic") is True


def test_is_third_party_domain_rejects_government_and_academic_tlds():
    """Hardening: 地方自治体(.lg.jp)/中央省庁(.go.jp)/大学・学校(.ac.jp/.ed.jp)は、
    民間クリニックが自ら保有するドメインではないため、fetch前からthird-party確定にする
    (100医院CanaryのHuman AuditでREVIEW止まりだったpref.mie.lg.jp等の実例に基づく)。"""
    assert is_third_party_domain("https://www.pref.mie.lg.jp/kenkot/hp/84853050446.htm") is True
    assert is_third_party_domain("https://www.city.takahama.lg.jp/soshiki/seisaku/15026.html") is True
    assert is_third_party_domain("https://www.yamagata-u.ac.jp/yamapura/") is True
    assert is_third_party_domain("kenkot@pref.mie.lg.jp") is True


def test_is_third_party_domain_does_not_block_or_jp_medical_corporations():
    """.or.jpは医療法人自身が保有するケースもあるため、TLDだけでは third-party にしない
    (ownership判定はcandidate_verifierのGate C=第三者組織キーワード検出に委ねる)。"""
    assert is_third_party_domain("https://www.some-iryouhoujin-hospital.or.jp/") is False


def test_adapter_caps_queries_at_max_queries():
    provider = StubProvider()
    adapter = SearchCandidateAdapter(provider, max_queries=1)
    result = adapter.run("サンプルクリニック", prefecture="東京都", city="渋谷区")
    assert len(provider.called_with) == 1
    assert result.queries_used == ["サンプルクリニック メール"]


def test_adapter_status_no_results_when_nothing_found():
    provider = StubProvider()
    adapter = SearchCandidateAdapter(provider, max_queries=3)
    result = adapter.run("サンプルクリニック")
    assert result.status == "NO_RESULTS"
    assert result.candidates == []


def test_adapter_status_ok_when_candidates_found():
    provider = StubProvider(by_query={"サンプルクリニック メール": [
        {"url": "https://clinic.example", "title": "", "content": "info@clinic.example"},
    ]})
    adapter = SearchCandidateAdapter(provider, max_queries=3)
    result = adapter.run("サンプルクリニック", include_disambiguation=False)
    assert result.status == "OK"
    assert [c.email for c in result.candidates] == ["info@clinic.example"]


def test_adapter_status_search_failed_when_all_queries_error():
    provider = StubProvider(raise_on={
        "サンプルクリニック メール", "サンプルクリニック メールアドレス", "サンプルクリニック お問い合わせ",
    })
    adapter = SearchCandidateAdapter(provider, max_queries=3)
    result = adapter.run("サンプルクリニック", include_disambiguation=False)
    assert result.status == "SEARCH_FAILED"
    assert len(result.errors) == 3


def test_adapter_partial_failure_still_ok_if_any_query_succeeds():
    provider = StubProvider(
        by_query={"サンプルクリニック メールアドレス": [
            {"url": "https://clinic.example", "title": "", "content": "info@clinic.example"},
        ]},
        raise_on={"サンプルクリニック メール"},
    )
    adapter = SearchCandidateAdapter(provider, max_queries=3)
    result = adapter.run("サンプルクリニック", include_disambiguation=False)
    assert result.status == "OK"
    assert len(result.errors) == 1
