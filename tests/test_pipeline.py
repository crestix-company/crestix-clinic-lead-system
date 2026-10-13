"""pipeline.py(オーケストレーター)のend-to-end unit test。実HTTP/実API接続は行わない。"""
from urllib.parse import urlsplit

from src.enrichment.email_research.pipeline import (
    STATUS_FETCH_FAILED,
    STATUS_FORM_ONLY,
    STATUS_NO_EMAIL,
    STATUS_REVIEW_FINAL,
    STATUS_SEARCH_CANDIDATE,
    STATUS_VERIFIED_EMAIL,
    run_pipeline,
)
from src.enrichment.safe_web import SafeFetcher, WebResponse
from src.enrichment.search_provider import SearchError


class StubTransport:
    def __init__(self, pages: dict[str, str]):
        self.pages = pages

    def get(self, url, timeout, max_bytes):
        path = urlsplit(url).path or "/"
        key = f"https://{urlsplit(url).hostname}{path}"
        if key in self.pages:
            return WebResponse(200, {"content-type": "text/html; charset=utf-8"}, self.pages[key].encode())
        return WebResponse(404, {"content-type": "text/html"}, b"<html>not found</html>")


class AllowAllFetcher(SafeFetcher):
    def allowed(self, url):
        return True


def _fetcher(pages):
    return AllowAllFetcher(transport=StubTransport(pages), interval=0)


class StubSearchProvider:
    def __init__(self, organic_by_query=None, raise_on_search=None, ai_overview=None, raise_on_raw=False):
        self.organic_by_query = organic_by_query or {}
        self.raise_on_search = raise_on_search or set()
        self.ai_overview = ai_overview
        self.raise_on_raw = raise_on_raw
        self.search_calls: list[str] = []
        self.search_raw_calls: list[str] = []

    def search(self, query):
        self.search_calls.append(query)
        if query in self.raise_on_search:
            raise SearchError("stub search error")
        return self.organic_by_query.get(query, [])

    def search_raw(self, query):
        self.search_raw_calls.append(query)
        if self.raise_on_raw:
            raise SearchError("stub ai overview error")
        if query in self.raise_on_search:
            raise SearchError("stub search error")
        payload = {"organic_results": self.organic_by_query.get(query, [])}
        if self.ai_overview is not None:
            payload["ai_overview"] = self.ai_overview
        return payload


def test_official_crawl_found_skips_search_entirely():
    pages = {
        "https://clinic.example/": (
            '<html><title>サンプルクリニック</title><body><a href="/contact">Contact</a></body></html>'
        ),
        "https://clinic.example/contact": (
            '<html><title>お問い合わせ | サンプルクリニック</title><body>'
            '<a href="mailto:info@clinic.example">Mail</a></body></html>'
        ),
    }
    provider = StubSearchProvider()
    result = run_pipeline(
        clinic_id="1", clinic_name="サンプルクリニック", hp_url="https://clinic.example/",
        official_crawl_eligible=True, search_provider=provider, fetcher=_fetcher(pages),
    )
    assert result.clinic_status == STATUS_VERIFIED_EMAIL
    assert [r.email for r in result.email_rows] == ["info@clinic.example"]
    assert result.email_rows[0].source_type == "OFFICIAL_HP"
    assert result.search_status == ""  # 検索は一切呼ばれていない


def test_no_hp_falls_back_to_search_and_gets_verified_on_official_reconfirmation():
    query = "サンプルクリニック メール"
    organic = {query: [
        {"url": "https://newly-found.example/contact", "title": "お問い合わせ | サンプルクリニック",
         "content": "連絡先 info@newly-found.example"},
    ]}
    pages = {
        "https://newly-found.example/contact": (
            '<html><title>お問い合わせ | サンプルクリニック</title><body>'
            '<a href="mailto:info@newly-found.example">info@newly-found.example</a></body></html>'
        ),
    }
    provider = StubSearchProvider(organic_by_query=organic)
    result = run_pipeline(
        clinic_id="2", clinic_name="サンプルクリニック", hp_url="", official_crawl_eligible=False,
        search_provider=provider, fetcher=_fetcher(pages), max_search_queries=1,
    )
    assert result.clinic_status == STATUS_VERIFIED_EMAIL
    assert result.email_rows[0].source_type == "SEARCH_QUERY"
    assert result.email_rows[0].verified_on_official is True


def test_search_candidate_unconfirmed_stays_search_candidate_not_verified():
    query = "サンプルクリニック メール"
    organic = {query: [
        {"url": "https://portal.example/listing/1", "title": "", "content": "info@clinic.example"},
    ]}
    provider = StubSearchProvider(organic_by_query=organic)
    result = run_pipeline(
        clinic_id="3", clinic_name="サンプルクリニック", hp_url="https://clinic.example/",
        official_crawl_eligible=True, search_provider=provider, fetcher=_fetcher({}), max_search_queries=1,
    )
    # official crawl: hp_url fetch fails(404) -> FETCH_FAILED, but a search candidate with
    # correct domain surfaced too -> should remain SEARCH_CANDIDATE (not verified, page 404).
    assert result.clinic_status == STATUS_SEARCH_CANDIDATE
    assert result.email_rows[0].status == STATUS_SEARCH_CANDIDATE


def test_third_party_candidate_never_becomes_verified_and_is_counted_separately():
    query = "サンプルクリニック メール"
    organic = {query: [
        {"url": "https://job-medley.com/clinic/sample", "title": "求人", "content": "recruit@job-medley.com"},
    ]}
    provider = StubSearchProvider(organic_by_query=organic)
    result = run_pipeline(
        clinic_id="4", clinic_name="サンプルクリニック", hp_url="", official_crawl_eligible=False,
        search_provider=provider, fetcher=_fetcher({}), max_search_queries=1,
    )
    assert result.rejected_third_party_count == 1
    assert result.email_rows == []
    assert result.clinic_status == STATUS_NO_EMAIL


def test_search_failure_does_not_become_no_email():
    provider = StubSearchProvider(raise_on_search={
        "サンプルクリニック メール", "サンプルクリニック メールアドレス", "サンプルクリニック お問い合わせ",
    })
    result = run_pipeline(
        clinic_id="5", clinic_name="サンプルクリニック", hp_url="", official_crawl_eligible=False,
        search_provider=provider, fetcher=_fetcher({}), max_search_queries=3,
    )
    assert result.search_status == "SEARCH_FAILED"
    assert result.clinic_status == STATUS_FETCH_FAILED  # NO_EMAILにはならない


def test_ai_overview_absent_is_normal_and_still_yields_no_email():
    provider = StubSearchProvider()  # organicもai_overviewも無し
    result = run_pipeline(
        clinic_id="6", clinic_name="サンプルクリニック", hp_url="", official_crawl_eligible=False,
        search_provider=provider, fetcher=_fetcher({}), max_search_queries=1,
    )
    assert result.search_status == "NO_RESULTS"
    assert result.ai_overview_status == "ABSENT"
    assert result.clinic_status == STATUS_NO_EMAIL


def test_ai_overview_error_is_isolated_but_still_prevents_no_email():
    provider = StubSearchProvider(raise_on_raw=True)
    result = run_pipeline(
        clinic_id="7", clinic_name="サンプルクリニック", hp_url="", official_crawl_eligible=False,
        search_provider=provider, fetcher=_fetcher({}), max_search_queries=1,
    )
    assert result.ai_overview_status == "ERROR"
    assert result.clinic_status == STATUS_FETCH_FAILED


def test_form_only_when_official_crawl_has_form_and_search_finds_nothing():
    pages = {
        "https://clinic.example/": '<html><title>Top</title><body><a href="/contact">Contact</a></body></html>',
        "https://clinic.example/contact": (
            '<html><title>お問い合わせ</title><body>'
            '<form action="/submit"><input name="name"></form></body></html>'
        ),
    }
    provider = StubSearchProvider()
    result = run_pipeline(
        clinic_id="8", clinic_name="サンプルクリニック", hp_url="https://clinic.example/",
        official_crawl_eligible=True, search_provider=provider, fetcher=_fetcher(pages), max_search_queries=1,
    )
    assert result.clinic_status == STATUS_FORM_ONLY


def test_review_status_when_verifier_flags_other_facility_suspected():
    query = "全く無関係の名称クリニック メール"
    organic = {query: [
        {"url": "https://clinic.example/contact", "title": "お問い合わせ", "content": "info@clinic.example"},
    ]}
    pages = {
        # official crawlのroot: リンク無し/メール無しでNO_EMAIL_FOUND -> known_hp_urlは維持されたまま検索へfallback。
        "https://clinic.example/": '<html><title>Top</title><body>ご案内のみ</body></html>',
        "https://clinic.example/contact": (
            '<html><title>お問い合わせ</title><body>'  # クリニック名を含まない
            '<a href="mailto:info@clinic.example">info@clinic.example</a></body></html>'
        ),
    }
    provider = StubSearchProvider(organic_by_query=organic)
    result = run_pipeline(
        clinic_id="9", clinic_name="全く無関係の名称クリニック", hp_url="https://clinic.example/",
        official_crawl_eligible=True,
        search_provider=provider, fetcher=_fetcher(pages), max_search_queries=1,
    )
    assert result.clinic_status == STATUS_REVIEW_FINAL


def test_query_escalation_stops_after_first_template_once_verified():
    """「メール」だけで確定できれば、「メールアドレス」「お問い合わせ」は一切発行しない。"""
    query = "サンプルクリニック メール"
    organic = {query: [
        {"url": "https://clinic.example/contact", "title": "お問い合わせ | サンプルクリニック",
         "content": "info@clinic.example"},
    ]}
    pages = {
        "https://clinic.example/contact": (
            '<html><title>お問い合わせ | サンプルクリニック</title><body>'
            '<a href="mailto:info@clinic.example">info@clinic.example</a></body></html>'
        ),
    }
    provider = StubSearchProvider(organic_by_query=organic)
    result = run_pipeline(
        clinic_id="10", clinic_name="サンプルクリニック", hp_url="", official_crawl_eligible=False,
        search_provider=provider, fetcher=_fetcher(pages), max_search_queries=6,
    )
    assert result.clinic_status == STATUS_VERIFIED_EMAIL
    assert provider.search_raw_calls == [query]  # 1件目(メール)のみ、search_rawでAI Overviewも同梱取得
    assert provider.search_calls == []  # メールアドレス/お問い合わせは一切発行しない
    assert result.ai_overview_status == ""  # VERIFIED確定後はAI Overviewの判定自体も行わない


def test_query_escalation_tries_next_template_when_earlier_ones_are_empty():
    """「メール」「メールアドレス」が0件のときだけ「お問い合わせ」まで進む。"""
    q1, q2, q3 = "サンプルクリニック メール", "サンプルクリニック メールアドレス", "サンプルクリニック お問い合わせ"
    organic = {q3: [
        {"url": "https://clinic.example/contact", "title": "お問い合わせ | サンプルクリニック",
         "content": "info@clinic.example"},
    ]}
    pages = {
        "https://clinic.example/contact": (
            '<html><title>お問い合わせ | サンプルクリニック</title><body>'
            '<a href="mailto:info@clinic.example">info@clinic.example</a></body></html>'
        ),
    }
    provider = StubSearchProvider(organic_by_query=organic)
    result = run_pipeline(
        clinic_id="11", clinic_name="サンプルクリニック", hp_url="", official_crawl_eligible=False,
        search_provider=provider, fetcher=_fetcher(pages), max_search_queries=6,
    )
    assert result.clinic_status == STATUS_VERIFIED_EMAIL
    assert provider.search_raw_calls == [q1]
    assert provider.search_calls == [q2, q3]  # 3テンプレート全て順に発行された


def test_disambiguation_query_only_issued_when_plain_query_is_empty_and_location_available():
    plain = "サンプルクリニック メール"
    disamb = "サンプルクリニック 東京都渋谷区 メール"
    organic = {disamb: [
        {"url": "https://clinic.example/contact", "title": "お問い合わせ | サンプルクリニック",
         "content": "info@clinic.example"},
    ]}
    pages = {
        "https://clinic.example/contact": (
            '<html><title>お問い合わせ | サンプルクリニック</title><body>'
            '<a href="mailto:info@clinic.example">info@clinic.example</a></body></html>'
        ),
    }
    provider = StubSearchProvider(organic_by_query=organic)
    result = run_pipeline(
        clinic_id="12", clinic_name="サンプルクリニック", hp_url="", official_crawl_eligible=False,
        prefecture="東京都", city="渋谷区",
        search_provider=provider, fetcher=_fetcher(pages), max_search_queries=6,
    )
    assert result.clinic_status == STATUS_VERIFIED_EMAIL
    assert provider.search_raw_calls == [plain]
    assert provider.search_calls == [disamb]  # plainが0件だったときだけ発行される


def test_disambiguation_not_issued_when_plain_query_already_has_candidates():
    plain = "サンプルクリニック メール"
    organic = {plain: [
        {"url": "https://job-medley.com/clinic/sample", "title": "求人", "content": "recruit@job-medley.com"},
    ]}
    provider = StubSearchProvider(organic_by_query=organic)
    result = run_pipeline(
        clinic_id="13", clinic_name="サンプルクリニック", hp_url="", official_crawl_eligible=False,
        prefecture="東京都", city="渋谷区",
        search_provider=provider, fetcher=_fetcher({}), max_search_queries=6,
    )
    # テンプレート1(メール)はplainで(third-partyだが)候補自体が見つかっているため、
    # テンプレート1についてのdisambiguationは発行しない(テンプレート2/3は各々plainが0件なので発行される)。
    assert "サンプルクリニック 東京都渋谷区 メール" not in provider.search_calls
    assert provider.search_calls == [
        "サンプルクリニック メールアドレス", "サンプルクリニック 東京都渋谷区 メールアドレス",
        "サンプルクリニック お問い合わせ", "サンプルクリニック 東京都渋谷区 お問い合わせ",
    ]


def test_third_party_rejection_captures_detail_row_for_human_audit():
    """Fresh Canary Human Audit用: third-party棄却の詳細(email/source_url/rationale)が
    rejected_rowsへ追加的に記録される(既存のrejected_third_party_count/email_rowsの
    挙動は変更しない)。"""
    query = "サンプルクリニック メール"
    organic = {query: [
        {"url": "https://job-medley.com/clinic/sample", "title": "求人", "content": "recruit@job-medley.com"},
    ]}
    provider = StubSearchProvider(organic_by_query=organic)
    result = run_pipeline(
        clinic_id="15", clinic_name="サンプルクリニック", hp_url="", official_crawl_eligible=False,
        search_provider=provider, fetcher=_fetcher({}), max_search_queries=1,
    )
    assert result.rejected_third_party_count == 1
    assert len(result.rejected_rows) == 1
    assert result.rejected_rows[0].email == "recruit@job-medley.com"
    assert result.rejected_rows[0].clinic_id == "15"
    assert result.email_rows == []  # 既存の挙動は不変


def test_known_portal_fp_suginonaika_not_verified_through_full_pipeline():
    """Problem A regression(Fresh 100-clinic Canary clinic_id=541 杉野内科クリニック):
    Production DBのhp_url自体がdoctors-search.com(複数医院掲載ポータル)を指していた
    実例。ページ内容は実際にcurlで取得した本物の文言(タイトル・住所・copyright)を
    使用する。Official Website Trust Gateがhp_url自体をNON_OFFICIALと判定し、
    webmaster@doctors-search.comをVERIFIED_EMAILにしない(third-partyとして棄却する)
    ことを保証する。"""
    pages = {
        "https://www.doctors-search.com/c-chuo/suginonaika/": (
            '<html><title>杉野内科クリニック　東京都中央区　中央区内科・消化器科・呼吸器科・循環器科</title>'
            '<body>＞<a href="https://www.doctors-search.com/">HOME</a>'
            '■掲載日：2003-02-04　■更新日：2005-07-29　〒104-0054　東京都中央区勝どき２－９－１２'
            '相模ビル３Ｆ　ＴＥＬ：03-5547-0961　ＦＡＸ：03-5547-0962'
            '■院長名　医学博士　杉野　敬一'
            '当サイトに記載しております文章、画像、写真などを無断で複写・転載することを禁じます。'
            'Copyright(c) doctors-search.com All Rights Reserved.</body></html>'
        ),
        "https://www.doctors-search.com/": (
            '<html><title>doctors-search.com 医院検索</title><body>'
            'お問い合わせは<a href="mailto:webmaster@doctors-search.com">webmaster@doctors-search.com</a>まで。'
            'Copyright(c) doctors-search.com All Rights Reserved.</body></html>'
        ),
    }
    provider = StubSearchProvider()
    result = run_pipeline(
        clinic_id="541", clinic_name="医療法人社団　健杏会　杉野内科クリニック",
        hp_url="https://www.doctors-search.com/c-chuo/suginonaika/", official_crawl_eligible=True,
        prefecture="東京都", city="中央区",
        search_provider=provider, fetcher=_fetcher(pages), max_search_queries=1,
    )
    assert result.official_trust_status == "NON_OFFICIAL"
    assert all(r.email != "webmaster@doctors-search.com" for r in result.email_rows)
    assert any(r.email == "webmaster@doctors-search.com" for r in result.rejected_rows)
    assert result.clinic_status != STATUS_VERIFIED_EMAIL


def test_official_confirmed_site_still_excludes_individual_third_party_org_email():
    """Part 2(OFFICIAL_HP上のEmail Ownership): サイト全体はOFFICIAL_CONFIRMEDでも、
    特定の1件のメールの周辺文脈が学会/協会等の第三者組織を示す場合は、そのメールだけを
    third-partyとして除外する(他の医院自身のメールはVERIFIED_EMAILのまま残る)。"""
    pages = {
        "https://clinic.example/": (
            '<html><title>サンプルクリニック</title><body>'
            '<p>お問い合わせは<a href="mailto:info@clinic.example">info@clinic.example</a>まで。</p>'
            '<p>日本医師会 事務局への連絡は'
            '<a href="mailto:ishikai@clinic.example">ishikai@clinic.example</a>まで。</p>'
            '</body></html>'
        ),
    }
    provider = StubSearchProvider()
    result = run_pipeline(
        clinic_id="20", clinic_name="サンプルクリニック", hp_url="https://clinic.example/",
        official_crawl_eligible=True, search_provider=provider, fetcher=_fetcher(pages), max_search_queries=1,
    )
    assert result.official_trust_status == "OFFICIAL_CONFIRMED"
    assert [r.email for r in result.email_rows] == ["info@clinic.example"]
    assert any(r.email == "ishikai@clinic.example" for r in result.rejected_rows)
    assert result.clinic_status == STATUS_VERIFIED_EMAIL


def test_known_wrong_clinic_fp_not_verified_through_full_pipeline():
    """FP-1 regression(100医院Canary): 伊東市「むらかみ小児科クリニック」に対し、富山市の
    別施設の予約ページを誤ってVERIFIED_EMAILにしていた。pipeline全体を通しても
    VERIFIED_EMAILへ昇格しないことを保証する。"""
    query = "むらかみ小児科クリニック メール"
    organic = {query: [
        {"url": "https://b.inet489.jp/mrkm0708/yoyaku/guide.cgi",
         "title": "むらかみ小児科アレルギークリニック 予約受付", "content": "mrkm0708@b.inet489.jp"},
    ]}
    pages = {
        "https://b.inet489.jp/mrkm0708/yoyaku/guide.cgi": (
            '<html><title>むらかみ小児科アレルギークリニック 予約受付</title><body>'
            '富山県富山市のむらかみ小児科アレルギークリニックのご予約はこちら。'
            '<a href="mailto:mrkm0708@b.inet489.jp">mrkm0708@b.inet489.jp</a></body></html>'
        ),
    }
    provider = StubSearchProvider(organic_by_query=organic)
    result = run_pipeline(
        clinic_id="54197", clinic_name="むらかみ小児科クリニック", hp_url="", official_crawl_eligible=False,
        prefecture="静岡県", city="伊東市", address="静岡県伊東市瓶山1-9-7",
        search_provider=provider, fetcher=_fetcher(pages), max_search_queries=1,
    )
    assert result.clinic_status != STATUS_VERIFIED_EMAIL
    assert all(r.status != STATUS_VERIFIED_EMAIL for r in result.email_rows)


def test_known_third_party_org_fp_not_verified_through_full_pipeline():
    """FP-2 regression(100医院Canary): 「医療法人社団憲生会 藤田小児科医院」に対し、
    日本東方医学会/東方医療振興財団側のメールを誤ってVERIFIED_EMAILにしていた。
    pipeline全体を通してもVERIFIED_EMAILへ昇格せず、third-partyとして棄却されることを保証する。"""
    query = "医療法人社団 憲生会 藤田小児科医院 メールアドレス"
    organic = {query: [
        {"url": "https://www.jptoho.or.jp/gakkai/certification/103.html",
         "title": "認定機関一覧 | 公益財団法人東方医療振興財団", "content": "zaidan@jptoho.or.jp"},
    ]}
    pages = {
        "https://www.jptoho.or.jp/gakkai/certification/103.html": (
            '<html><title>認定機関一覧 | 公益財団法人東方医療振興財団</title><body>'
            '日本東方医学会 事務局からのお知らせ。認定機関: 医療法人社団憲生会 藤田小児科医院。'
            'お問い合わせは<a href="mailto:zaidan@jptoho.or.jp">zaidan@jptoho.or.jp</a>まで。'
            '</body></html>'
        ),
    }
    provider = StubSearchProvider(organic_by_query=organic)
    result = run_pipeline(
        clinic_id="31746", clinic_name="医療法人社団 憲生会 藤田小児科医院", hp_url="",
        official_crawl_eligible=False, prefecture="千葉県", city="市川市",
        search_provider=provider, fetcher=_fetcher(pages), max_search_queries=6,
    )
    assert result.clinic_status != STATUS_VERIFIED_EMAIL
    assert all(r.status != STATUS_VERIFIED_EMAIL for r in result.email_rows)
    assert result.rejected_third_party_count >= 1


def test_known_true_positive_kazenoki_style_official_crawl_preserved():
    """既知のTrue Positive保存テスト: 風の木クリニックスタイルの公式サイト(mailtoが複数掲載)は、
    Hardening後もOFFICIAL_HP crawl経由で全てVERIFIED_EMAILのまま残る。"""
    pages = {
        "https://www.kazenoki.jp/index.html": (
            '<html><title>風の木クリニック</title><body>'
            '<a href="mailto:counseling1@kazenoki.jp">counseling1@kazenoki.jp</a>'
            '<a href="mailto:counseling3@kazenoki.jp">counseling3@kazenoki.jp</a>'
            '<a href="mailto:counseling4@kazenoki.jp">counseling4@kazenoki.jp</a>'
            '<a href="mailto:counseling6@kazenoki.jp">counseling6@kazenoki.jp</a>'
            '<a href="mailto:counseling7@kazenoki.jp">counseling7@kazenoki.jp</a>'
            '<a href="mailto:counseling11@kazenoki.jp">counseling11@kazenoki.jp</a>'
            '<a href="mailto:counseling12@kazenoki.jp">counseling12@kazenoki.jp</a>'
            '<a href="mailto:mail@kazenoki.jp">mail@kazenoki.jp</a>'
            '</body></html>'
        ),
    }
    provider = StubSearchProvider()
    result = run_pipeline(
        clinic_id="236", clinic_name="風の木クリニック", hp_url="https://www.kazenoki.jp/index.html",
        official_crawl_eligible=True, prefecture="東京都", city="千代田区",
        search_provider=provider, fetcher=_fetcher(pages), max_search_queries=1,
    )
    assert result.clinic_status == STATUS_VERIFIED_EMAIL
    assert len(result.email_rows) == 8
    assert all(r.status == STATUS_VERIFIED_EMAIL and r.source_type == "OFFICIAL_HP" for r in result.email_rows)
    assert result.search_status == ""  # 公式crawlでVERIFIED済みのため検索は呼ばれない


def test_ai_overview_piggybacked_from_first_call_without_extra_request():
    """1件目をsearch_rawで取得できた場合、AI Overview専用の追加コールは発生しない。"""
    provider = StubSearchProvider(ai_overview={"text_blocks": [{"snippet": "特にメール記載なし"}]})
    result = run_pipeline(
        clinic_id="14", clinic_name="サンプルクリニック", hp_url="", official_crawl_eligible=False,
        search_provider=provider, fetcher=_fetcher({}), max_search_queries=6,
    )
    assert result.ai_overview_status == "ABSENT"  # AI Overviewはあったが候補メールは無かった
    assert provider.search_raw_calls == ["サンプルクリニック メール"]  # 1回だけ(ai_overview用の追加コール無し)
    assert provider.search_calls == ["サンプルクリニック メールアドレス", "サンプルクリニック お問い合わせ"]


def test_fast_acquisition_uses_one_formal_is_query_and_reuses_ai_payload():
    provider = StubSearchProvider(ai_overview={"text_blocks": [{"snippet": "メール記載なし"}]})
    result = run_pipeline(
        clinic_id="is-1", clinic_name="小川クリニック", prefecture="東京都", city="千代田区",
        search_provider=provider, fetcher=_fetcher({}), fast_acquisition_mode=True,
    )
    assert provider.search_raw_calls == ["小川クリニック メール 東京都 千代田区"]
    assert provider.search_calls == []
    assert result.search_status == "NO_RESULTS"
    assert result.ai_overview_status == "ABSENT"
