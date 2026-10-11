"""OFFICIAL_HP crawl結果に対する信頼性確認(Official Website Trust Gate)。

Hardening Phase 2(Problem A対応): official_hp_email_crawler.crawl_clinic()は、
Production DB(clinics.sqlite3)のhp_status='VERIFIED'だけを信用してhp_urlを起点に
crawlしていたが、hp_url自体が「複数医院を掲載する第三者ポータル/ディレクトリサイト」を
指している可能性を一度も確認していなかった。

実例(Fresh 100-clinic Canary clinic_id=541 杉野内科クリニック):
  hp_url = https://www.doctors-search.com/c-chuo/suginonaika/
  実際のページはタイトルに医院名(杉野内科クリニック)を含み、住所・電話・院長名まで
  正確に掲載していた(= 単純な「医院名が本文に無い」チェックでは検知できない)。
  しかし footer に "Copyright(c) doctors-search.com All Rights Reserved." とあり、
  著作権者がクリニックではなくポータル運営者(ドメイン名そのもの)だった。
  候補メール webmaster@doctors-search.com はポータル運営者のメールであり、
  杉野内科クリニック自身の所有ではない。

本モジュールは、crawl済みページの集約テキスト(crawled_text_sample)に対して
以下のGateを順に評価する(ブロックリストだけに依存しない):
  1. hp_urlのdomainが既知のthird-partyブロックリストに合致 -> NON_OFFICIAL
  2. ページ内容がポータル/ディレクトリ/第三者組織を示すキーワードを含む -> NON_OFFICIAL
  3. footer/copyright表記が、医院名ではなくサイト自身のdomain名(運営者)を指している
     -> NON_OFFICIAL
  4. 異なる都道府県名が明記されている(所在地相違の疑い) -> REVIEW_OFFICIALITY
  5. 医院名(正式名称の核)がページ内に確認できない -> REVIEW_OFFICIALITY
  6. 上記すべてクリア -> OFFICIAL_CONFIRMED

迷う場合は常にOFFICIAL_CONFIRMEDにしない(Conservative Promotion)。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from src.enrichment.email_research.clinic_identity import (
    _core_clinic_name,
    _location_contradiction,
)
from src.enrichment.email_research.search_candidate_adapter import is_third_party_domain
from src.enrichment.hp_analysis import host as site_host
from src.master.email_sales_eligibility import is_public_municipal_clinic, source_owner_type

TRUST_GATE_VERSION = "official-site-trust-0.2.0"

STATUS_OFFICIAL_CONFIRMED = "OFFICIAL_CONFIRMED"
STATUS_NON_OFFICIAL = "NON_OFFICIAL"
STATUS_REVIEW_OFFICIALITY = "REVIEW_OFFICIALITY"

# 医療ポータル/医院検索/口コミ/求人/予約代行プラットフォーム等を示す語彙
# (search_candidate_adapter.is_third_party_domainのdomainブロックリストでは
# 捕捉できない、未知ドメインの内容ベース判定用)。
_PORTAL_CONTENT_KEYWORD_RE = re.compile(
    "医院検索|病院検索|クリニック検索|医療機関検索|医療機関情報|医療機関を検索|"
    "口コミ|ランキング|全国の医療機関|掲載医院|掲載医療機関|医院(?:の)?一覧|病院(?:の)?一覧|"
    "クリニック(?:の)?一覧|エリアから探す|条件から探す|掲載日|求人サイト|予約プラットフォーム|"
    "認定機関一覧|認定医療機関一覧|会員名簿|会員一覧|"
    # Hardening Phase 3: 「〇〇を探す/調べる」「総合サイト」は医院自身の公式サイトが使う表現では
    # なく、複数医院の中から利用者に検索させるポータル/ディレクトリサイト特有の表現
    # (実例1: nihon-byouin.jp「病院を探す」「都道府県から病院を探す」。
    #  実例2: bihokuikiiki.jp「医療機関・薬局を調べる」「医療・福祉・介護の総合サイト」)。
    "病院を探す|クリニックを探す|医院を探す|歯科医院を探す|歯科クリニックを探す|"
    "地図から.{0,4}探す|都道府県から.{0,4}探す|診療科目から.{0,4}探す|"
    "医療機関.{0,2}を調べる|医院.{0,2}を調べる|病院.{0,2}を調べる|総合サイト"
)

_REGISTRY_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9.\-]{2,}")
_COPYRIGHT_RE = re.compile(r"copyright\s*(?:\(?c\)?|©|©)?\s*\d{0,4}\s*([^\s　]{2,60})", re.I)


def _registrable_root(host: str) -> str:
    h = (host or "").lower().strip().rstrip(".")
    if h.startswith("www."):
        h = h[4:]
    return h.split(".")[0] if h else ""


def _copyright_points_to_site_itself_not_clinic(page_text: str, clinic_name: str, hp_url: str) -> bool:
    """footer/copyright表記の運営者名が、医院名ではなくサイト自身のdomain名(運営者)を
    指しているかを判定する(doctors-search.comの実例: "Copyright(c) doctors-search.com")。"""
    text = unicodedata.normalize("NFKC", page_text or "")
    match = _COPYRIGHT_RE.search(text)
    if not match:
        return False
    holder = match.group(1)
    core = _core_clinic_name(clinic_name)
    if core and core in holder:
        return False
    root = _registrable_root(site_host(hp_url))
    return bool(root) and len(root) >= 3 and root.lower() in holder.lower()


@dataclass(frozen=True)
class TrustOutcome:
    status: str
    rationale: str
    trust_gate_version: str = TRUST_GATE_VERSION


def assess_official_trust(
    *, hp_url: str, clinic_name: str, prefecture: str = "", crawled_text_sample: str = "",
) -> TrustOutcome:
    """OFFICIAL_HP crawlで取得済みのページ集約テキストに対しTrust Gateを評価する。

    ネットワークアクセスは行わない(すでに取得済みのテキストだけを見る純粋関数)。
    """
    if is_third_party_domain(hp_url):
        return TrustOutcome(
            status=STATUS_NON_OFFICIAL,
            rationale="hp_urlのdomainが既知の第三者ドメインブロックリストに合致",
        )

    # clinic_id=99522インシデント: hp_urlが自治体の医療機関ディレクトリ(city.ashibetsu.
    # hokkaido.jp)を指していた。対象医院自身が市立/町立/村立/県立/公立等でない限り、
    # 自治体/政府ドメインはクリニック自身の公式サイトではない。
    if (source_owner_type(source_url=hp_url, clinic_name=clinic_name) == "GOVERNMENT"
            and not is_public_municipal_clinic(clinic_name)):
        return TrustOutcome(
            status=STATUS_NON_OFFICIAL,
            rationale="hp_urlのdomainが自治体/政府ドメインであり、対象医院自身が市立/町立/村立/県立/公立等でない",
        )

    if _PORTAL_CONTENT_KEYWORD_RE.search(unicodedata.normalize("NFKC", crawled_text_sample or "")):
        return TrustOutcome(
            status=STATUS_NON_OFFICIAL,
            rationale="ページ内容が医療ポータル/医院検索/ディレクトリを示すキーワードを含む",
        )

    # 注意: 学会/財団/協会等のキーワードは、サイト全体(集約テキスト)に対しては使わない。
    # 「日本医師会認定産業医」のような医師の肩書き・資格表記は、医院自身の公式サイトにも
    # ごく普通に現れるため、サイト全体スキャンでは third-party と誤判定してしまう
    # (この粒度のチェックはPart 2のper-emailチェック=個々のメールのnearby_textに対してのみ行う)。

    if _copyright_points_to_site_itself_not_clinic(crawled_text_sample, clinic_name, hp_url):
        return TrustOutcome(
            status=STATUS_NON_OFFICIAL,
            rationale="footer/copyright表記の運営者名が医院名ではなくサイト自身のdomain名"
                      "(ポータル運営者)を指している",
        )

    if _location_contradiction(crawled_text_sample, prefecture):
        return TrustOutcome(
            status=STATUS_REVIEW_OFFICIALITY,
            rationale="ページ内に異なる都道府県名が明記されており所在地相違の疑い",
        )

    core = _core_clinic_name(clinic_name)
    normalized_text = unicodedata.normalize("NFKC", crawled_text_sample or "")
    if core and core not in normalized_text:
        return TrustOutcome(
            status=STATUS_REVIEW_OFFICIALITY,
            rationale="ページ内に医院名(正式名称の核)が確認できない",
        )

    return TrustOutcome(
        status=STATUS_OFFICIAL_CONFIRMED,
        rationale="domainブロックリスト/ポータルキーワード/第三者組織/著作権者ミスマッチ/"
                  "所在地矛盾のいずれにも該当せず、医院名を確認できた",
    )
