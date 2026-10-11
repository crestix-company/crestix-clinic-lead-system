"""official_site_trust.py(Official Website Trust Gate)のunit test。実HTTP接続は行わない。"""
from src.enrichment.email_research.official_site_trust import (
    STATUS_NON_OFFICIAL,
    STATUS_OFFICIAL_CONFIRMED,
    STATUS_REVIEW_OFFICIALITY,
    assess_official_trust,
)


def test_portal_url_in_blocklist_is_rejected_without_content_check():
    """Hardening Phase 2 Problem A regression: doctors-search.com(ポータル)はdomain
    ブロックリストだけでNON_OFFICIALに確定する(実際のCanaryでVERIFIED_EMAILまで
    到達していたclinic_id=541 杉野内科クリニックの実例)。"""
    out = assess_official_trust(
        hp_url="https://www.doctors-search.com/c-chuo/suginonaika/",
        clinic_name="杉野内科クリニック", prefecture="東京都",
        crawled_text_sample="杉野内科クリニック 東京都中央区 内科",
    )
    assert out.status == STATUS_NON_OFFICIAL


def test_clinic_official_url_with_matching_identity_passes():
    """医院名・所在地が一致し、第三者組織/ポータルの兆候が無ければOFFICIAL_CONFIRMED。"""
    out = assess_official_trust(
        hp_url="https://www.kazenoki.jp/index.html", clinic_name="風の木クリニック",
        prefecture="東京都",
        crawled_text_sample="風の木クリニック トップページ 東京都千代田区 counseling1@kazenoki.jp",
    )
    assert out.status == STATUS_OFFICIAL_CONFIRMED


def test_unknown_domain_portal_keyword_content_is_rejected():
    """blocklist未登録の未知ドメインでも、「医院検索」等のポータル特有の語彙があれば
    NON_OFFICIALにする(blocklistだけに依存しない要件)。"""
    out = assess_official_trust(
        hp_url="https://www.unknown-portal-example.com/tokyo/sample-clinic/",
        clinic_name="サンプルクリニック", prefecture="東京都",
        crawled_text_sample="サンプルクリニック 全国の医療機関検索 エリアから探す 口コミ・ランキング",
    )
    assert out.status == STATUS_NON_OFFICIAL


def test_copyright_mismatch_with_site_own_domain_is_rejected():
    """footerのcopyright表記がクリニック名ではなくサイト自身のdomain名(運営者)を
    指している場合はNON_OFFICIAL(doctors-search.comの実例の一般化版)。"""
    out = assess_official_trust(
        hp_url="https://www.some-unknown-portal-xyz.com/c-chuo/suginonaika/",
        clinic_name="杉野内科クリニック", prefecture="東京都",
        crawled_text_sample=(
            "杉野内科クリニック 東京都中央区 内科 住所 東京都中央区勝どき2-9-12 "
            "電話 03-5547-0961 Copyright(c) some-unknown-portal-xyz.com All Rights Reserved."
        ),
    )
    assert out.status == STATUS_NON_OFFICIAL


def test_different_prefecture_mentioned_is_review_officiality_not_outright_reject():
    """同名/類似名クリニックが別の都道府県に実在する疑いがある場合は、断定的な
    NON_OFFICIALではなくREVIEW_OFFICIALITY(人手確認)に留める。"""
    out = assess_official_trust(
        hp_url="https://www.seki-shika.com/", clinic_name="関山歯科医院", prefecture="茨城県",
        crawled_text_sample="関歯科医院 熊本市西区の歯医者です。インプラント・歯科矯正はお任せください。",
    )
    assert out.status == STATUS_REVIEW_OFFICIALITY


def test_clinic_name_absent_is_review_officiality():
    """医院名がページ内に全く確認できない場合もREVIEW_OFFICIALITY(断定しない)。"""
    out = assess_official_trust(
        hp_url="https://www.newly-set-up-clinic.example/", clinic_name="サンプルクリニック",
        prefecture="", crawled_text_sample="ようこそ お問い合わせはこちら",
    )
    assert out.status == STATUS_REVIEW_OFFICIALITY


def test_certification_listing_portal_keyword_is_rejected():
    """「認定機関一覧」等の会員名簿/証明書一覧ページ特有のキーワードがあればNON_OFFICIAL
    (Hardening Phase 1のFP-2=jptoho.or.jpの実例に基づく)。
    注意: 「学会/財団/協会」という単語自体は、医師の肩書き(例:日本医師会認定産業医)として
    医院自身の公式サイトにも普通に現れるため、サイト全体スキャンでは third-party 判定の
    決め手にしない(Part 2のper-emailチェックに限定する)。"""
    out = assess_official_trust(
        hp_url="https://www.jptoho.or.jp/", clinic_name="藤田小児科医院", prefecture="千葉県",
        crawled_text_sample="日本東方医学会 公益財団法人東方医療振興財団 事務局 認定機関一覧",
    )
    assert out.status == STATUS_NON_OFFICIAL


def test_clinic_own_site_mentioning_medical_association_credential_is_not_rejected():
    """医院自身の公式サイトが医師の資格(例:日本医師会認定産業医)を記載していても、
    それだけでNON_OFFICIALにしない(サイト全体スキャンでの誤検知防止の回帰テスト)。"""
    out = assess_official_trust(
        hp_url="https://clinic.example/", clinic_name="サンプルクリニック", prefecture="東京都",
        crawled_text_sample="サンプルクリニック 東京都 院長は日本医師会認定産業医です。",
    )
    assert out.status == STATUS_OFFICIAL_CONFIRMED
