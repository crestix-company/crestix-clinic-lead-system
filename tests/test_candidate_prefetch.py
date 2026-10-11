from src.enrichment.email_research.candidate_prefetch import (
    STATUS_CLINIC_RECRUITMENT_DETAIL,
    STATUS_DEFAULT,
    STATUS_TRUSTED_PUBLIC_MEDICAL_DETAIL,
    assess_candidate_source,
)


def test_trusted_public_medical_detail_passes_prefetch():
    result = assess_candidate_source(
        clinic_name="つやま歯科・口腔外科医院", prefecture="福岡県",
        address="福岡県久留米市藤山町745-4", candidate_email="tsuyama@d.nifty.jp",
        source_url="https://www.iryou.teikyouseido.mhlw.go.jp/znk-web/juminkanja/S2430/initialize?"
                   "kikanCd=0002731293&kikanKbn=3&prefCd=40",
        source_title="つやま歯科・口腔外科医院ツヤマシカコウクウゲカイイン",
        snippet_text="医療機関概要 住所 福岡県久留米市藤山町745ー4 案内用電子メール tsuyama@d.nifty.jp",
    )
    assert result.status == STATUS_TRUSTED_PUBLIC_MEDICAL_DETAIL
    assert result.allow_fetch is True


def test_government_list_page_remains_rejected():
    result = assess_candidate_source(
        clinic_name="サンプルクリニック", prefecture="東京都", address="東京都千代田区1-1",
        source_url="https://www.city.example.lg.jp/medical/clinic-list.pdf",
        source_title="市内医療機関一覧", snippet_text="複数医院の一覧",
    )
    assert result.status == STATUS_DEFAULT
    assert result.allow_fetch is False


def test_clinic_recruitment_detail_passes_prefetch_only():
    result = assess_candidate_source(
        clinic_name="天王台駅前歯科", candidate_email="tennoudai.recruit0801@gmail.com",
        source_url="https://clients.itszai.jp/x/recruitments/279291/entry-forms",
        source_title="PMTC注力の歯科衛生士のエントリーフォーム - 天王台駅前歯科",
        snippet_text="天王台駅前歯科 採用サイト メールアドレス",
    )
    assert result.status == STATUS_CLINIC_RECRUITMENT_DETAIL
    assert result.allow_fetch is True


def test_legacy_tochigi_public_medical_detail_passes_prefetch():
    """Phase 5 A: 旧都道府県システムも限定host/path/施設ID/医院同定で許可する。"""
    result = assess_candidate_source(
        clinic_name="栃木産科婦人科医院", prefecture="栃木県", city="足利市",
        address="栃木県足利市錦町14", candidate_email="totigiog@crest.ocn.ne.jp",
        source_url="https://www.qq.pref.tochigi.lg.jp/pb_md_index/pb_md_list/pb_md_info/1294?fileout=0",
        source_title="栃木産科婦人科医院", snippet_text="栃木県足利市 医療機関詳細 電子メール",
    )
    assert result.status == STATUS_TRUSTED_PUBLIC_MEDICAL_DETAIL
    assert result.allow_fetch is True


def test_national_public_detail_accepts_formal_long_clinic_name():
    """Phase 5 B: 法人名・診療科が挿入された正式長形式でも固有名を保守的に照合する。"""
    result = assess_candidate_source(
        clinic_name="銀座並木通りクリニック", prefecture="東京都", city="中央区",
        candidate_email="psim.iisy02@gmail.com",
        source_url=("https://www.iryou.teikyouseido.mhlw.go.jp/znk-web/juminkanja/S2430/initialize"
                    "?kikanCd=2136925869&kikanKbn=2&prefCd=13"),
        source_title="医療法人社団 創文会 銀座並木通り心療内科・内科クリニック",
        snippet_text="東京都中央区 医療機関詳細 案内用電子メール",
    )
    assert result.status == STATUS_TRUSTED_PUBLIC_MEDICAL_DETAIL
    assert result.allow_fetch is True


def test_legacy_public_list_and_arbitrary_lg_page_remain_rejected():
    list_result = assess_candidate_source(
        clinic_name="栃木産科婦人科医院", prefecture="栃木県",
        source_url="https://www.qq.pref.tochigi.lg.jp/pb_md_index/pb_md_list/",
        source_title="医療機関一覧 栃木産科婦人科医院",
    )
    arbitrary_result = assess_candidate_source(
        clinic_name="栃木産科婦人科医院", prefecture="栃木県",
        source_url="https://www.pref.tochigi.lg.jp/medical/index.html",
        source_title="県の医療制度 栃木産科婦人科医院",
    )
    assert list_result.status == STATUS_DEFAULT
    assert arbitrary_result.status == STATUS_DEFAULT


def test_recruitment_same_name_wrong_location_is_rejected_before_fetch():
    """Phase 5 D: 同名医院でも求人の所在地が別都道府県なら例外にしない。"""
    result = assess_candidate_source(
        clinic_name="篠原医院", prefecture="茨城県", city="日立市",
        address="茨城県日立市多賀町", candidate_email="shinohara@kind.ocn.ne.jp",
        source_url="https://www.hellowork.mhlw.go.jp/kensaku/detail/recruit",
        source_title="篠原医院 求人情報", snippet_text="熊本県葦北郡芦北町の篠原医院 採用",
    )
    assert result.status == STATUS_DEFAULT
    assert result.allow_fetch is False
    assert "所在地" in result.reason


def test_recruitment_same_clinic_location_can_fetch():
    """Phase 5 E: 対象所在地を確認できる医院専用採用detailはfetch対象。"""
    result = assess_candidate_source(
        clinic_name="天王台駅前歯科", prefecture="千葉県", city="我孫子市",
        address="千葉県我孫子市天王台1-5-1", candidate_email="tennoudai.recruit0801@gmail.com",
        source_url="https://clients.itszai.jp/x/recruitments/279291/entry-forms",
        source_title="天王台駅前歯科 採用", snippet_text="千葉県我孫子市 天王台駅前歯科 求人",
    )
    assert result.status == STATUS_CLINIC_RECRUITMENT_DETAIL
    assert result.allow_fetch is True
