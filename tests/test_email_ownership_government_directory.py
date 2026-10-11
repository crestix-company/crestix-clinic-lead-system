"""Government/municipal directory ownership fixtures.

clinic_id=99522 incident (Production Canary 100, 2026-10-11): あむデンタルクリニック's
AUTO_TRUE Primary resolved to kaigoserv@city.ashibetsu.hokkaido.jp (芦別市 介護高齢課
地域包括支援係), a municipal government contact, not the clinic's own. Root cause: the
pre-existing government-domain check only covered the dedicated .lg.jp/.go.jp TLDs; this
host uses Japan's common city.<name>.<prefecture>.jp municipal naming convention, which has
no .lg.jp/.go.jp suffix, and discovery_source=="OFFICIAL_HP" (public.clinics.hp_url) was
being treated as ownership evidence on its own (Source Owner != Email Owner).

Real HTTP is never used; StubTransport/AllowAllFetcher mirror tests/test_candidate_verifier.py.
"""
from urllib.parse import urlsplit

from src.enrichment.email_research.candidate_verifier import (
    STATUS_REJECTED_THIRD_PARTY,
    STATUS_VERIFIED,
    verify_candidate,
)
from src.enrichment.email_research.official_site_trust import (
    STATUS_NON_OFFICIAL,
    STATUS_OFFICIAL_CONFIRMED,
    assess_official_trust,
)
from src.enrichment.safe_web import SafeFetcher, WebResponse
from src.master.email_sales_eligibility import is_government_host, is_public_municipal_clinic


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


# --- is_government_host / is_public_municipal_clinic unit coverage ---------

def test_is_government_host_covers_municipal_non_lg_jp_pattern():
    assert is_government_host("city.ashibetsu.hokkaido.jp") is True
    assert is_government_host("town.sample.nagano.jp") is True
    assert is_government_host("vill.sample.okinawa.jp") is True
    assert is_government_host("pref.mie.jp") is True
    assert is_government_host("pref.mie.lg.jp") is True
    assert is_government_host("kaigoserv.city.ashibetsu.hokkaido.jp") is False  # not the municipal prefix itself


def test_is_government_host_false_for_ordinary_clinic_domain():
    assert is_government_host("kanda-uchida.jp") is False
    assert is_government_host("hamanasugeka.com") is False


def test_is_public_municipal_clinic_detects_known_markers():
    assert is_public_municipal_clinic("喜界町国民健康保険診療所") is True
    assert is_public_municipal_clinic("市立札幌病院") is True
    assert is_public_municipal_clinic("あむデンタルクリニック歯科口腔外科") is False


# --- clinic_id=99522 fixture (exact incident shape) -------------------------

_ASHIBETSU_URL = "https://www.city.ashibetsu.hokkaido.jp/docs/7476.html"
_ASHIBETSU_PAGE = (
    '<html><title>医療機関一覧 | 芦別市</title><body>'
    '<h2>あむデンタルクリニック歯科口腔外科</h2>'
    '<p>介護高齢課 地域包括支援係</p>'
    '<a href="mailto:kaigoserv@city.ashibetsu.hokkaido.jp">kaigoserv@city.ashibetsu.hokkaido.jp</a>'
    '</body></html>'
)


def test_clinic_id_99522_fixture_trust_gate_rejects_municipal_hp_url():
    """Page-level Trust Gate: a private Clinic's hp_url pointing at a municipal
    facility directory is NON_OFFICIAL, not OFFICIAL_CONFIRMED."""
    out = assess_official_trust(
        hp_url=_ASHIBETSU_URL, clinic_name="あむデンタルクリニック歯科口腔外科",
        prefecture="北海道", crawled_text_sample=_ASHIBETSU_PAGE,
    )
    assert out.status == STATUS_NON_OFFICIAL


def test_clinic_id_99522_fixture_end_to_end_not_auto_true_not_primary():
    """End-to-end: the city-department email must not verify as the clinic's own."""
    outcome = verify_candidate(
        "kaigoserv@city.ashibetsu.hokkaido.jp", "あむデンタルクリニック歯科口腔外科",
        known_hp_url=_ASHIBETSU_URL, candidate_source_url=_ASHIBETSU_URL,
        fetcher=_fetcher({_ASHIBETSU_URL: _ASHIBETSU_PAGE}),
    )
    assert outcome.status != STATUS_VERIFIED
    assert outcome.status == STATUS_REJECTED_THIRD_PARTY


# --- Adversarial scenarios A-F ----------------------------------------------

def test_scenario_a_municipality_directory_city_staff_email_blocked():
    """A: private clinic + municipality directory + city staff email -> reject/block."""
    url = "https://www.town-sample.saitama.jp/kenko/clinics/42.html"
    page = (
        '<html><title>町内医療機関案内 | サンプル町</title><body>'
        '<h2>さくらクリニック</h2><p>健康福祉課 保健センター</p>'
        '<a href="mailto:hoken@town-sample.saitama.jp">hoken@town-sample.saitama.jp</a>'
        '</body></html>'
    )
    outcome = verify_candidate(
        "hoken@town-sample.saitama.jp", "さくらクリニック",
        known_hp_url=url, candidate_source_url=url, fetcher=_fetcher({url: page}),
    )
    assert outcome.status == STATUS_REJECTED_THIRD_PARTY


def test_scenario_b_medical_association_directory_office_email_blocked():
    """B: private clinic + medical association directory + association office email -> reject/block."""
    pages = {
        "https://example-ishikai.or.jp/member/list.html": (
            '<html><title>会員名簿 | サンプル市医師会</title><body>'
            '会員: ひまわりクリニック。事務局への連絡は'
            '<a href="mailto:office@example-ishikai.or.jp">office@example-ishikai.or.jp</a>まで。'
            '</body></html>'
        ),
    }
    outcome = verify_candidate(
        "office@example-ishikai.or.jp", "ひまわりクリニック",
        known_hp_url="", candidate_source_url="https://example-ishikai.or.jp/member/list.html",
        fetcher=_fetcher(pages),
    )
    assert outcome.status == STATUS_REJECTED_THIRD_PARTY


def test_scenario_c_portal_support_email_blocked():
    """C: private clinic + portal + portal support email -> reject/block (pre-existing blocklist)."""
    outcome = verify_candidate(
        "support@doctorsfile.jp", "たんぽぽクリニック",
        known_hp_url="https://clinic.example/", candidate_source_url="https://doctorsfile.jp/clinic/sample",
        fetcher=_fetcher({}),
    )
    assert outcome.status == STATUS_REJECTED_THIRD_PARTY


def test_scenario_d_trusted_public_directory_clinic_specific_email_allowed():
    """D: clinic detail on 医療情報ネット(MHLW) + clinic-specific email -> allow ownership
    verification (Source Owner may be a trusted directory while Email Owner is still the
    clinic itself; must not be blanket-blocked just because the host is .go.jp)."""
    url = ("https://www.iryou.teikyouseido.mhlw.go.jp/znk-web/juminkanja/S2430/initialize"
           "?kikanCd=0009999999&kikanKbn=3&prefCd=01")
    pages = {
        "https://www.iryou.teikyouseido.mhlw.go.jp/znk-web/juminkanja/S2430/initialize": (
            '<html><title>たんぽぽ歯科医院</title><body><h1>たんぽぽ歯科医院</h1>'
            '<p>北海道札幌市中央区1-1-1</p><h2>案内用電子メールアドレス</h2>'
            '<p>tanpopo@d.example.jp</p></body></html>'
        ),
    }
    outcome = verify_candidate(
        "tanpopo@d.example.jp", "たんぽぽ歯科医院", known_hp_url="", candidate_source_url=url,
        source_title="たんぽぽ歯科医院タンポポシカイイン",
        snippet_text="住所 北海道札幌市中央区1-1-1 案内用電子メール tanpopo@d.example.jp",
        fetcher=_fetcher(pages), prefecture="北海道", address="北海道札幌市中央区1-1-1",
    )
    assert outcome.status == STATUS_VERIFIED


def test_scenario_e_municipal_clinic_official_domain_not_blanket_rejected():
    """E: municipal Clinic + municipality official domain + clinic-specific contact ->
    must not be blanket-rejected purely for being on a government/municipal domain."""
    url = "https://www.city.sample-town.hokkaido.jp/shiritsubyoin/"
    page = (
        '<html><title>市立サンプル病院</title><body><h1>市立サンプル病院</h1>'
        '<p>北海道サンプル町1-1</p>'
        '<a href="mailto:somu@city.sample-town.hokkaido.jp">somu@city.sample-town.hokkaido.jp</a>'
        '</body></html>'
    )
    out = assess_official_trust(
        hp_url=url, clinic_name="市立サンプル病院", prefecture="北海道",
        crawled_text_sample=page,
    )
    # Not blanket-NON_OFFICIAL solely for being a government/municipal domain: the target
    # Clinic is itself municipally operated, so the Trust Gate must not short-circuit here.
    assert out.status == STATUS_OFFICIAL_CONFIRMED


def test_scenario_f_same_medical_corporation_shared_email_allowed():
    """F: same medical corporation + shared corporate email -> existing policy allows."""
    pages = {
        "https://kenshokai-group.example/contact": (
            '<html><title>お問い合わせ | 医療法人健生会</title><body>'
            '医療法人健生会 さくら台クリニック'
            '<a href="mailto:info@kenshokai-group.example">info@kenshokai-group.example</a>'
            '</body></html>'
        ),
    }
    outcome = verify_candidate(
        "info@kenshokai-group.example", "医療法人健生会 さくら台クリニック",
        known_hp_url="https://kenshokai-group.example/", candidate_source_url="https://kenshokai-group.example/contact",
        fetcher=_fetcher(pages),
    )
    assert outcome.status == STATUS_VERIFIED
