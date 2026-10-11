"""candidate_verifier.py のunit test。実HTTP接続は行わず、SafeFetcherへstub transportを注入する。"""
from urllib.parse import urlsplit

import pytest

from src.enrichment.email_research.candidate_verifier import (
    STATUS_DOMAIN_MISMATCH,
    STATUS_NO_OFFICIAL_REFERENCE,
    STATUS_REJECTED_THIRD_PARTY,
    STATUS_REVIEW,
    STATUS_UNCONFIRMED,
    STATUS_VERIFIED,
    STATUS_VERIFY_FETCH_FAILED,
    verify_candidate,
)
from src.enrichment.safe_web import SafeFetcher, WebResponse


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


def test_rejects_known_job_portal_domain_without_fetching():
    outcome = verify_candidate(
        "recruit@job-medley.com", "サンプルクリニック",
        known_hp_url="https://clinic.example/", candidate_source_url="https://job-medley.com/clinic/sample",
        fetcher=_fetcher({}),  # 何も登録しなくても到達しないはず
    )
    assert outcome.status == STATUS_REJECTED_THIRD_PARTY
    assert outcome.verified_on_official is False


def test_external_email_on_known_official_page_still_requires_page_confirmation():
    outcome = verify_candidate(
        "info@other-domain.example", "サンプルクリニック",
        known_hp_url="https://clinic.example/", candidate_source_url="https://clinic.example/contact",
        fetcher=_fetcher({}),
    )
    assert outcome.status == STATUS_VERIFY_FETCH_FAILED
    assert outcome.verified_on_official is False


def test_freemail_is_not_promoted_without_fetch_and_identity_confirmation():
    outcome = verify_candidate(
        "sampleclinic@gmail.com", "サンプルクリニック",
        known_hp_url="https://clinic.example/", candidate_source_url="https://clinic.example/recruit",
        fetcher=_fetcher({}),
    )
    assert outcome.status == STATUS_VERIFY_FETCH_FAILED
    assert outcome.verified_on_official is False


def test_official_clinic_page_can_verify_explicitly_published_freemail():
    """Phase 5 C: 公式ページ上の問い合わせfree-mailをdomain差だけで棄却しない。"""
    url = "https://www.sophiaclinic.jp/legal/"
    pages = {url: (
        '<html><title>特定商取引法に基づく表記 | 表参道ソフィアクリニック</title><body>'
        '<h1>表参道ソフィアクリニック</h1><p>東京都港区北青山3丁目</p>'
        '<h2>お問い合わせ</h2><p><a href="mailto:sophiaclinic7@yahoo.co.jp">'
        'sophiaclinic7@yahoo.co.jp</a></p></body></html>'
    )}
    outcome = verify_candidate(
        "sophiaclinic7@yahoo.co.jp", "表参道ソフィアクリニック",
        known_hp_url="https://www.sophiaclinic.jp/", candidate_source_url=url, fetcher=_fetcher(pages),
        prefecture="東京都", city="港区", address="東京都港区北青山3丁目",
    )
    assert outcome.status == STATUS_VERIFIED
    assert outcome.verified_on_official is True


def test_verified_when_official_contact_page_confirms_mailto():
    pages = {
        "https://clinic.example/contact": (
            '<html><title>お問い合わせ | サンプルクリニック</title><body>ご連絡は'
            '<a href="mailto:info@clinic.example">info@clinic.example</a></body></html>'
        ),
    }
    outcome = verify_candidate(
        "info@clinic.example", "サンプルクリニック",
        known_hp_url="https://clinic.example/", candidate_source_url="https://clinic.example/contact",
        fetcher=_fetcher(pages),
    )
    assert outcome.status == STATUS_VERIFIED
    assert outcome.verified_on_official is True
    assert outcome.official_domain == "clinic.example"


def test_content_page_blog_is_not_used_for_confirmation():
    pages = {
        "https://clinic.example/blog/": (
            '<html><title>ブログ</title><body>'
            '<a href="mailto:info@clinic.example">info@clinic.example</a></body></html>'
        ),
    }
    outcome = verify_candidate(
        "info@clinic.example", "サンプルクリニック",
        known_hp_url="https://clinic.example/", candidate_source_url="https://clinic.example/blog/",
        fetcher=_fetcher(pages),
    )
    assert outcome.status == STATUS_UNCONFIRMED
    assert outcome.verified_on_official is False


def test_review_when_clinic_name_absent_from_confirmed_page():
    pages = {
        "https://clinic.example/contact": (
            '<html><title>お問い合わせ</title><body>'
            '<a href="mailto:info@clinic.example">info@clinic.example</a>'
            '<p>当グループの他施設一覧はこちら</p></body></html>'
        ),
    }
    outcome = verify_candidate(
        "info@clinic.example", "全く無関係の名称クリニック",
        known_hp_url="https://clinic.example/", candidate_source_url="https://clinic.example/contact",
        fetcher=_fetcher(pages),
    )
    assert outcome.status == STATUS_REVIEW
    assert outcome.verified_on_official is False


def test_email_not_present_on_fetched_page_is_unconfirmed():
    pages = {"https://clinic.example/contact": '<html><title>お問い合わせ</title><body>フォームのみ</body></html>'}
    outcome = verify_candidate(
        "info@clinic.example", "サンプルクリニック",
        known_hp_url="https://clinic.example/", candidate_source_url="https://clinic.example/contact",
        fetcher=_fetcher(pages),
    )
    assert outcome.status == STATUS_UNCONFIRMED


def test_fetch_failure_does_not_raise_and_stays_unverified():
    outcome = verify_candidate(
        "info@clinic.example", "サンプルクリニック",
        known_hp_url="https://clinic.example/", candidate_source_url="https://clinic.example/contact",
        fetcher=_fetcher({}),  # 404 -> WebError
    )
    assert outcome.status == STATUS_VERIFY_FETCH_FAILED
    assert outcome.verified_on_official is False


def test_no_known_hp_but_source_domain_self_consistent_can_verify():
    pages = {
        "https://newly-found-clinic.example/contact": (
            '<html><title>お問い合わせ | サンプルクリニック</title><body>ご連絡は'
            '<a href="mailto:info@newly-found-clinic.example">info@newly-found-clinic.example</a></body></html>'
        ),
    }
    outcome = verify_candidate(
        "info@newly-found-clinic.example", "サンプルクリニック",
        known_hp_url="", candidate_source_url="https://newly-found-clinic.example/contact",
        fetcher=_fetcher(pages),
    )
    assert outcome.status == STATUS_VERIFIED
    assert outcome.official_domain == "newly-found-clinic.example"


def test_no_known_hp_and_source_domain_mismatch_cannot_verify():
    outcome = verify_candidate(
        "info@some-domain.example", "サンプルクリニック",
        known_hp_url="", candidate_source_url="https://portal.example/listing/123",
        fetcher=_fetcher({}),
    )
    assert outcome.status == STATUS_NO_OFFICIAL_REFERENCE


def test_known_wrong_clinic_fp_rejected_by_exact_core_name_and_location_gate():
    """FP-1 regression: 伊東市「むらかみ小児科クリニック」に対し、富山市「むらかみ小児科
    アレルギークリニック」の予約ページを誤採用していた既知のFalse Positive。
    旧ロジック(医院名の先頭4文字一致だけ)ではこの別施設を見抜けなかった。
    Gate A(医院名の核の完全一致 + 都道府県の矛盾)でVERIFIEDへの昇格を防ぐ。
    """
    pages = {
        "https://b.inet489.jp/mrkm0708/yoyaku/guide.cgi": (
            '<html><title>むらかみ小児科アレルギークリニック 予約受付</title><body>'
            '富山県富山市のむらかみ小児科アレルギークリニックのご予約はこちら。'
            '<a href="mailto:mrkm0708@b.inet489.jp">mrkm0708@b.inet489.jp</a></body></html>'
        ),
    }
    outcome = verify_candidate(
        "mrkm0708@b.inet489.jp", "むらかみ小児科クリニック",
        known_hp_url="", candidate_source_url="https://b.inet489.jp/mrkm0708/yoyaku/guide.cgi",
        fetcher=_fetcher(pages), prefecture="静岡県", city="伊東市", address="静岡県伊東市瓶山1-9-7",
    )
    assert outcome.status != STATUS_VERIFIED
    assert outcome.verified_on_official is False


def test_known_third_party_org_fp_rejected_even_when_clinic_name_appears_on_page():
    """FP-2 regression: 「医療法人社団憲生会 藤田小児科医院」に対し、日本東方医学会/
    東方医療振興財団側のzaidan@jptoho.or.jpを誤採用していた既知のFalse Positive。
    ページ内にクリニック名自体は(認定機関一覧として)載っており、名称一致だけでは
    防げない。Gate C(ページ内容が学会/財団等の第三者組織を示す)で棄却する。
    """
    pages = {
        "https://www.jptoho.or.jp/gakkai/certification/103.html": (
            '<html><title>認定機関一覧 | 公益財団法人東方医療振興財団</title><body>'
            '日本東方医学会 事務局からのお知らせ。認定機関: 医療法人社団憲生会 藤田小児科医院。'
            'お問い合わせは<a href="mailto:zaidan@jptoho.or.jp">zaidan@jptoho.or.jp</a>まで。'
            '</body></html>'
        ),
    }
    outcome = verify_candidate(
        "zaidan@jptoho.or.jp", "医療法人社団 憲生会 藤田小児科医院",
        known_hp_url="", candidate_source_url="https://www.jptoho.or.jp/gakkai/certification/103.html",
        fetcher=_fetcher(pages), prefecture="千葉県", city="市川市",
    )
    assert outcome.status == STATUS_REJECTED_THIRD_PARTY
    assert outcome.verified_on_official is False


def test_known_portal_fp_nihon_byouin_rejected_by_trust_gate_on_new_domain_branch():
    """Fresh Canary v3 regression: 「いまはし歯科クリニック」に対し、医院検索ポータル
    nihon-byouin.jp(タイトル形式「医院名｜所在地｜アクセス・診療時間・予約」はdoctors-search.com
    と同型)上のsupport@nihon-byouin.jpを誤採用していた既知のthird-party FP。
    実際のページ内容(curlで確認済み: 「都道府県から病院を探す」等のポータル語彙 +
    「Copyright © 2026 病院のご案内」)を再現したfixtureを使い、known_hp_urlが無い
    「新規発見domain」分岐にもOfficial Site Trust Gateが適用され、third-partyとして
    棄却されることを保証する。
    """
    pages = {
        "https://nihon-byouin.jp/imahashi-dental-clinic/": (
            '<html><title>いまはし歯科クリニック｜愛知県長久手市｜アクセス・診療時間・予約・'
            '外来受付・休診日</title><body>病院のご案内 病院を探す 地図から病院を探す '
            '都道府県から病院を探す 診療科目から病院を探す 歯科クリニックを探す '
            'いまはし歯科クリニック 愛知県長久手市 '
            '<a href="mailto:support@nihon-byouin.jp">support@nihon-byouin.jp</a>'
            '<footer>&copy; 2026 病院のご案内</footer></body></html>'
        ),
    }
    outcome = verify_candidate(
        "support@nihon-byouin.jp", "いまはし歯科クリニック",
        known_hp_url="", candidate_source_url="https://nihon-byouin.jp/imahashi-dental-clinic/",
        fetcher=_fetcher(pages), prefecture="愛知県", city="長久手市",
    )
    assert outcome.status != STATUS_VERIFIED
    assert outcome.verified_on_official is False


def test_known_portal_fp_bihokuikiiki_rejected_by_trust_gate_on_new_domain_branch():
    """Fresh Canary v3 regression: 「細川歯科医院」に対し、地域医療ネットワークポータル
    bihokuikiiki.jp(ページタイトル「細川歯科医院 - びほくいきいきネット」)上の
    m.ishikai@bihokuikiiki.jpを誤採用していた既知のthird-party FP。実際のページ内容
    (curlで確認済み: 「医療機関・薬局を調べる」「医療・福祉・介護の総合サイト」等の
    ポータル語彙 + 「Copyright © 2026 びほくいきいきネット」)を再現したfixtureを使い、
    third-partyとして棄却されることを保証する。
    """
    pages = {
        "https://bihokuikiiki.jp/dentistry/861/": (
            '<html><title>細川歯科医院 - びほくいきいきネット</title><body>'
            '広島県備北地域の医療・福祉・介護の総合サイト 医療機関・薬局を調べる '
            '介護・福祉サービスを調べる 細川歯科医院 '
            '三次地区医師会 '
            '<a href="mailto:m.ishikai@bihokuikiiki.jp">m.ishikai@bihokuikiiki.jp</a>'
            '<footer>Copyright &copy; 2026 びほくいきいきネット, All Rights Reserved.</footer>'
            '</body></html>'
        ),
    }
    outcome = verify_candidate(
        "m.ishikai@bihokuikiiki.jp", "細川歯科医院",
        known_hp_url="", candidate_source_url="https://bihokuikiiki.jp/dentistry/861/",
        fetcher=_fetcher(pages), prefecture="岩手県", city="盛岡市",
    )
    assert outcome.status != STATUS_VERIFIED
    assert outcome.verified_on_official is False


def test_known_true_positive_photon_group_contact_preserved_on_new_domain_branch():
    """Fresh Canary v3 known TP preservation: 「医療法人社団光輝会 麻布光輝クリニック」に対する
    hc@photon.or.jp(姉妹院ひかりクリニックのページに同一法人の共通窓口として明記)は、
    Official Site Trust Gateを新規発見domain分岐へ追加した後もVERIFIEDのまま維持される
    ことを保証する(ポータル誤検知の巻き添えにしない)。
    """
    pages = {
        "https://www.photon.or.jp/hikariclinic/index.html": (
            '<html><title>ひかりクリニック</title><body>'
            '医療法人社団 光輝会 ひかりクリニック 東京都福生市本町95-3 '
            'TEL: 042-530-0221 '
            '<a href="mailto:hc@photon.or.jp">hc@photon.or.jp</a> '
            '麻布光輝クリニック 麻布光輝クリニック ひかりクリニック ひかりクリニック 光輝会 光輝会'
            '<footer>Copyright(c) 2014 光輝会</footer></body></html>'
        ),
    }
    outcome = verify_candidate(
        "hc@photon.or.jp", "医療法人社団　光輝会　麻布光輝クリニック",
        known_hp_url="", candidate_source_url="https://www.photon.or.jp/hikariclinic/index.html",
        fetcher=_fetcher(pages), prefecture="東京都",
    )
    assert outcome.status == STATUS_VERIFIED
    assert outcome.verified_on_official is True


def test_kaneko_style_page_match_but_foreign_email_owner_is_rejected():
    """Phase 4 A: 一覧に対象医院名があっても、メール周辺が別病院ならVERIFIED不可。"""
    pages = {
        "https://www.saimiya.com/recruit-top/resident-room/training.html": (
            '<html><title>済生会宇都宮病院 - 初期研修</title><body>'
            '<h1>済生会宇都宮病院 初期研修・医師募集</h1>'
            '<section><h2>研修担当</h2><p>keito_suto@saimiya.com</p></section>'
            '<h2>研修協力施設一覧</h2><p>金子内科医院 栃木市吹上町1642-1</p>'
            '</body></html>'
        ),
    }
    outcome = verify_candidate(
        "keito_suto@saimiya.com", "金子内科医院", known_hp_url="",
        candidate_source_url="https://www.saimiya.com/recruit-top/resident-room/training.html",
        source_title="済生会宇都宮病院 - 金子内科医院を含む研修施設一覧",
        snippet_text="メール keito_suto@saimiya.com 金子内科医院",
        fetcher=_fetcher(pages), prefecture="栃木県", address="栃木県栃木市吹上町1642-1",
    )
    assert outcome.status == STATUS_REJECTED_THIRD_PARTY
    assert outcome.verified_on_official is False
    assert "Email Ownership Gate" in outcome.rationale


def test_trusted_public_medical_detail_can_fetch_and_verify_exact_clinic():
    """Phase 4 B: 名称・住所・都道府県・施設IDが一致する単独detailのみ通す。"""
    url = ("https://www.iryou.teikyouseido.mhlw.go.jp/znk-web/juminkanja/S2430/initialize"
           "?kikanCd=0002731293&kikanKbn=3&prefCd=40")
    pages = {
        "https://www.iryou.teikyouseido.mhlw.go.jp/znk-web/juminkanja/S2430/initialize": (
            '<html><title>つやま歯科・口腔外科医院</title><body><h1>つやま歯科・口腔外科医院</h1>'
            '<p>福岡県久留米市藤山町745-4</p><h2>案内用電子メールアドレス</h2>'
            '<p>tsuyama@d.nifty.jp</p></body></html>'
        ),
    }
    outcome = verify_candidate(
        "tsuyama@d.nifty.jp", "つやま歯科・口腔外科医院", known_hp_url="", candidate_source_url=url,
        source_title="つやま歯科・口腔外科医院ツヤマシカコウクウゲカイイン",
        snippet_text="住所 福岡県久留米市藤山町745-4 案内用電子メール tsuyama@d.nifty.jp",
        fetcher=_fetcher(pages), prefecture="福岡県", address="福岡県久留米市藤山町745-4",
    )
    assert outcome.status == STATUS_VERIFIED


def test_clinic_recruitment_detail_verifies_but_role_is_non_sales_and_non_primary():
    """Phase 4 D: fetchは許可するがRECRUITはSales Primaryに入らない。"""
    from src.enrichment.email_research.email_role_classifier import classify_email_role
    from src.enrichment.email_research.primary_email_selector import EmailCandidateInput, select_primary_email

    url = "https://clients.itszai.jp/x/recruitments/279291/entry-forms"
    pages = {url: ('<html><title>天王台駅前歯科 採用サイト</title><body>'
                   '<h1>天王台駅前歯科 歯科衛生士採用</h1>'
                   '<p>千葉県我孫子市天王台1-5-1</p><h2>採用担当</h2>'
                   '<p>tennoudai.recruit0801@gmail.com</p></body></html>')}
    outcome = verify_candidate(
        "tennoudai.recruit0801@gmail.com", "天王台駅前歯科", known_hp_url="", candidate_source_url=url,
        source_title="歯科衛生士エントリー - 天王台駅前歯科",
        snippet_text="千葉県我孫子市 天王台駅前歯科 採用サイト メール tennoudai.recruit0801@gmail.com",
        fetcher=_fetcher(pages), prefecture="千葉県", address="千葉県我孫子市天王台1-5-1-1階",
    )
    assert outcome.status == STATUS_VERIFIED
    role = classify_email_role(
        "tennoudai.recruit0801@gmail.com", section_heading=outcome.email_context.section_heading,
        nearby_text=outcome.email_context.nearby_text, page_url=url,
        page_title=outcome.email_context.page_title,
    )
    assert role.role == "RECRUIT"
    assert role.sales_usable == "NO"
    primary = select_primary_email([EmailCandidateInput(
        clinic_id="112211", clinic_name="天王台駅前歯科", email="tennoudai.recruit0801@gmail.com",
        status="VERIFIED_EMAIL", source_type="SEARCH_QUERY", source_url=url,
        verified_on_official=True, third_party_suspected=False, email_role=role.role,
        role_confidence=role.role_confidence, sales_usable=role.sales_usable,
    )])
    assert primary.primary_email == ""


def test_distant_medical_association_credential_does_not_reject_clinic_email():
    """Phase 5 F: メールから離れた医師会資格語彙を所有者signalにしない。"""
    url = "https://www.hama-mental.com/"
    pages = {url: (
        '<html><title>浜松町メンタルクリニック</title><body>'
        '<section><h2>ご予約はこちら</h2><p><a href="mailto:yoyaku@hama-mental.com">'
        'yoyaku@hama-mental.com</a></p></section>'
        '<div><p>院長略歴</p><p>日本医師会認定産業医</p></div>'
        '</body></html>'
    )}
    outcome = verify_candidate(
        "yoyaku@hama-mental.com", "浜松町メンタルクリニック",
        known_hp_url=url, candidate_source_url=url, fetcher=_fetcher(pages), prefecture="東京都",
    )
    assert outcome.status == STATUS_VERIFIED
    assert outcome.verified_on_official is True


def test_known_true_positive_kazenoki_style_official_contact_still_verified():
    """既知のTrue Positive(風の木クリニック: 公式サイト掲載の複数mailtoがHuman AuditでTP確認済み)
    を、Hardening後も壊さないことの保存テスト。known_hp_url分岐でもGate A/Cは通過できる。
    """
    pages = {
        "https://www.kazenoki.jp/index.html": (
            '<html><title>風の木クリニック | トップページ</title><body>'
            '東京都千代田区のカウンセリングのご相談は'
            '<a href="mailto:counseling1@kazenoki.jp">counseling1@kazenoki.jp</a>まで。'
            '</body></html>'
        ),
    }
    outcome = verify_candidate(
        "counseling1@kazenoki.jp", "風の木クリニック",
        known_hp_url="https://www.kazenoki.jp/index.html",
        candidate_source_url="https://www.kazenoki.jp/index.html",
        fetcher=_fetcher(pages), prefecture="東京都", city="千代田区",
    )
    assert outcome.status == STATUS_VERIFIED
    assert outcome.verified_on_official is True


def test_gate_a_identity_rejects_location_contradiction_even_with_name_match():
    """Gate A: 医院名の核は一致していても、ページ内に別の都道府県名が明記されていれば
    所在地相違の疑いとしてVERIFIEDへ昇格させない。"""
    pages = {
        "https://newly-found-clinic.example/contact": (
            '<html><title>サンプルクリニック | 富山県富山市</title><body>'
            '<a href="mailto:info@newly-found-clinic.example">info@newly-found-clinic.example</a>'
            '</body></html>'
        ),
    }
    outcome = verify_candidate(
        "info@newly-found-clinic.example", "サンプルクリニック",
        known_hp_url="", candidate_source_url="https://newly-found-clinic.example/contact",
        fetcher=_fetcher(pages), prefecture="静岡県",
    )
    assert outcome.status == STATUS_REVIEW
    assert outcome.verified_on_official is False


def test_context_propagation_fix_location_contradiction_branch_carries_context():
    """Email Context Capture Fix regression: Fresh Canary v2で発見されたバグ
    (Gate Aの所在地矛盾REVIEW分岐がnearby_text/email_contextをVerificationOutcomeへ
    伝播し忘れていた)が再発しないことを確認する。"""
    pages = {
        "https://newly-found-clinic.example/contact": (
            '<html><title>サンプルクリニック | 富山県富山市</title><body>'
            '<h2>お問い合わせ</h2><p>ご連絡は'
            '<a href="mailto:info@newly-found-clinic.example">info@newly-found-clinic.example</a>'
            'まで。</p></body></html>'
        ),
    }
    outcome = verify_candidate(
        "info@newly-found-clinic.example", "サンプルクリニック",
        known_hp_url="", candidate_source_url="https://newly-found-clinic.example/contact",
        fetcher=_fetcher(pages), prefecture="静岡県",
    )
    assert outcome.status == STATUS_REVIEW
    assert outcome.email_context is not None
    assert outcome.nearby_text != ""
    assert "お問い合わせ" in outcome.nearby_text


def test_context_propagation_fix_core_name_absent_branch_carries_context():
    """Email Context Capture Fix regression: Gate Aの医院名不在REVIEW分岐についても同様に
    nearby_text/email_contextが伝播することを確認する。"""
    pages = {
        "https://clinic.example/contact": (
            '<html><title>お問い合わせ</title><body>'  # クリニック名を含まない
            '<h2>お問い合わせ</h2><p>ご連絡は'
            '<a href="mailto:info@clinic.example">info@clinic.example</a>まで。</p>'
            '</body></html>'
        ),
    }
    outcome = verify_candidate(
        "info@clinic.example", "全く無関係の名称クリニック",
        known_hp_url="https://clinic.example/", candidate_source_url="https://clinic.example/contact",
        fetcher=_fetcher(pages),
    )
    assert outcome.status == STATUS_REVIEW
    assert outcome.email_context is not None
    assert outcome.nearby_text != ""


def test_gate_a_identity_accepts_exact_core_name_with_legal_entity_prefix():
    """Gate A: 「医療法人社団X会 Y医院」のような法人格+法人名+施設名の表記でも、
    最後のトークン(施設名の核)が完全一致すればVERIFIEDできる(名称判定が厳しすぎて
    正当なケースまで落とさないことの確認)。"""
    pages = {
        "https://fujita-clinic.example/contact": (
            '<html><title>お問い合わせ | 藤田小児科医院</title><body>'
            '<a href="mailto:info@fujita-clinic.example">info@fujita-clinic.example</a>'
            '</body></html>'
        ),
    }
    outcome = verify_candidate(
        "info@fujita-clinic.example", "医療法人社団 憲生会 藤田小児科医院",
        known_hp_url="", candidate_source_url="https://fujita-clinic.example/contact",
        fetcher=_fetcher(pages),
    )
    assert outcome.status == STATUS_VERIFIED


def test_gate_c_ownership_rejects_medical_association_domain_even_with_exact_name_match():
    """Gate C: 医師会(第三者の職能団体)のページに医院名が完全一致で載っていても、
    そのメールは医師会自身のものである可能性が高く、VERIFIEDへ昇格させない。"""
    pages = {
        "https://example-ishikai.or.jp/member/list.html": (
            '<html><title>会員名簿 | 市川市医師会</title><body>'
            '会員: サンプルクリニック。事務局への連絡は'
            '<a href="mailto:info@example-ishikai.or.jp">info@example-ishikai.or.jp</a>まで。'
            '</body></html>'
        ),
    }
    outcome = verify_candidate(
        "info@example-ishikai.or.jp", "サンプルクリニック",
        known_hp_url="", candidate_source_url="https://example-ishikai.or.jp/member/list.html",
        fetcher=_fetcher(pages),
    )
    assert outcome.status == STATUS_REJECTED_THIRD_PARTY


def test_gate_b_weak_evidence_directory_listing_without_official_domain_stays_unverified():
    """Gate B: 公式domainが未知で、第三者の紹介サイト/ディレクトリらしきURLに候補メールの
    domainが一致しない場合は、弱い証拠のみでVERIFIEDにしない(既存のNO_OFFICIAL_REFERENCE
    判定が壊れていないことの確認)。"""
    outcome = verify_candidate(
        "info@clinic-own-domain.example", "サンプルクリニック",
        known_hp_url="", candidate_source_url="https://directory.example/clinics/sample-clinic",
        fetcher=_fetcher({}),
    )
    assert outcome.status == STATUS_NO_OFFICIAL_REFERENCE
    assert outcome.verified_on_official is False


def test_gov_and_academic_domains_are_rejected_as_third_party_without_fetching():
    """Hardening: 100医院CanaryのHuman Auditで実際にREVIEW止まりだった、地方自治体(.lg.jp)/
    大学(.ac.jp)ドメインは、fetchする前からthird-partyとして確定できる(is_third_party_domain
    のTLD拡張)。"""
    outcome = verify_candidate(
        "kenkot@pref.mie.lg.jp", "栢原歯科医院",
        known_hp_url="", candidate_source_url="https://www.pref.mie.lg.jp/kenkot/hp/84853050446.htm",
        fetcher=_fetcher({}),  # 何も登録しなくても到達しないはず(fetch前に棄却される)
    )
    assert outcome.status == STATUS_REJECTED_THIRD_PARTY


def test_mailto_display_conflict_is_not_confirmed():
    pages = {
        "https://clinic.example/contact": (
            '<html><title>お問い合わせ</title><body>'
            '<a href="mailto:person@clinic.example">info@clinic.example</a></body></html>'
        ),
    }
    outcome = verify_candidate(
        "person@clinic.example", "サンプルクリニック",
        known_hp_url="https://clinic.example/", candidate_source_url="https://clinic.example/contact",
        fetcher=_fetcher(pages),
    )
    assert outcome.status == STATUS_UNCONFIRMED
