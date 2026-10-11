"""検索/AI Overviewで見つかったcandidateを、公式HP側で再確認するだけのモジュール。

設計方針(ユーザー要件に対応):
- SerpAPI検索結果・AI Overview・snippetだけではVERIFIED_EMAILにしない。
- 再確認は「公式HP mailto」「公式HP本文」「公式お問い合わせページ」「公式法人ページ」からだけ行う
  (= SafeFetcherで実際にそのページを取得し、候補メールが明示的に存在するかを確認する)。
- 既存エンジン(official_hp_email_crawler/email_extraction)の判定ロジックを再利用し、
  車輪の再発明をしない。第三者サイト判定はsearch_candidate_adapter.is_third_party_domain
  (内部でhp_analysis.NON_OFFICIALを再利用)を使う。
- VERIFIED_EMAILにする条件を厳しくする(fail-closed):
    * candidateのメールdomainが公式hostと異なる場合も、既知の公式ページそのものに掲載され、
      Clinic IdentityとEmail Ownershipを満たすときだけ確認対象にする。
    * 再取得したページがblog/news等のcontent page(official_hp_email_crawler._is_content_page)
      ではないこと。
    * 再取得したページから、mailto優先順位で候補メールが実際に抽出できること。
- 第三者ドメイン(求人/ポータル/Web制作会社等)の候補は、一切fetchせずに即座に
  SEARCH_CANDIDATE止まりとする(third-party false positive = 0を保証するための保険)。

Hardening Phase(100医院Canaryの人手監査で確認された2件のFalse Positiveを受けて追加):
  FP-1: 伊東市「むらかみ小児科クリニック」に対し、富山市「むらかみ小児科アレルギークリニック」の
        予約ページ(別host、名称一部一致)を誤採用。
  FP-2: 「医療法人社団憲生会 藤田小児科医院」に対し、日本東方医学会/東方医療振興財団側の
        zaidan@jptoho.or.jp を誤採用(ページ内にクリニック名は載っていたが、メールの所有者は
        クリニックではなく第三者の学会/財団だった)。
  いずれも旧ロジック(looks_like_other_facility:医院名の先頭4文字が本文に無ければ別施設疑い)
  では防げなかった。VERIFIED_EMAILへ昇格する前に、以下Three Gatesを明示的に通す:
    Gate A (Clinic Identity):  医院名(正式名称の核となる部分)の完全一致 + 都道府県の矛盾が無いこと。
                               単なる名称一部一致だけでは昇格させない。
    Gate B (Official Domain):  既存のdomain一致/content-page判定(強い証拠 vs 弱い証拠)。
    Gate C (Email Ownership):  ページ内容が学会/財団/協会/連合会/事務局/医師会等の第三者組織を
                               示す場合は、ドメイン・名称が一致していてもVERIFIED禁止
                               (REJECTED_THIRD_PARTYへ落とす)。
  迷う場合は常にVERIFIEDへ上げずREVIEW/SEARCH_CANDIDATEへ落とす(Conservative Promotion)。
"""
from __future__ import annotations

from dataclasses import dataclass

from src.enrichment.email_research.clinic_identity import (
    OWNERSHIP_MATCH,
    OWNERSHIP_MISMATCH,
    assess_email_ownership,
    _core_name_present,
    _location_contradiction,
)
import unicodedata
import re
from src.enrichment.email_research.candidate_prefetch import (
    STATUS_CLINIC_RECRUITMENT_DETAIL,
    STATUS_DEFAULT as PREFETCH_DEFAULT,
    STATUS_TRUSTED_PUBLIC_MEDICAL_DETAIL,
    _address_match,
    assess_candidate_source,
)
from src.enrichment.email_research.email_context_extractor import EmailContext, extract_email_context
from src.enrichment.email_research.email_extraction import dedupe_emails
from src.enrichment.email_research.official_hp_email_crawler import (
    _email_domain_matches_site,
    _extract_non_article_emails,
    _is_content_page,
    _mailto_display_conflicts,
)
from src.enrichment.email_research.official_site_trust import (
    STATUS_NON_OFFICIAL as _TRUST_NON_OFFICIAL,
    STATUS_REVIEW_OFFICIALITY as _TRUST_REVIEW_OFFICIALITY,
    assess_official_trust,
)
from src.enrichment.email_research.search_candidate_adapter import is_third_party_domain
from src.enrichment.hp_analysis import host as site_host
from src.enrichment.safe_web import WebError

VERIFIER_VERSION = "candidate-verifier-0.5.0"

STATUS_VERIFIED = "VERIFIED"
STATUS_REVIEW = "REVIEW"
STATUS_REJECTED_THIRD_PARTY = "REJECTED_THIRD_PARTY"
STATUS_DOMAIN_MISMATCH = "DOMAIN_MISMATCH"
STATUS_NO_OFFICIAL_REFERENCE = "NO_OFFICIAL_REFERENCE"
STATUS_UNCONFIRMED = "UNCONFIRMED_ON_OFFICIAL_PAGE"
STATUS_VERIFY_FETCH_FAILED = "VERIFY_FETCH_FAILED"


def _registrable_host(value: str) -> str:
    value = (value or "").lower().strip().rstrip(".")
    return value[4:] if value.startswith("www.") else value


def _is_same_or_subdomain(candidate_host: str, official_host: str) -> bool:
    a, b = _registrable_host(candidate_host), _registrable_host(official_host)
    return bool(a and b) and (a == b or a.endswith("." + b))


@dataclass(frozen=True)
class VerificationOutcome:
    status: str
    verified_on_official: bool = False
    official_domain: str = ""
    verified_page_url: str = ""
    evidence_text: str = ""
    rationale: str = ""
    verifier_version: str = VERIFIER_VERSION
    nearby_text: str = ""  # Hardening Phase 2: Role Classification用の周辺文脈(追加専用field)
    email_context: EmailContext | None = None  # Fresh Canary v2: Context Capture全field(追加専用)


def verify_candidate(
    candidate_email: str,
    clinic_name: str,
    *,
    known_hp_url: str = "",
    candidate_source_url: str = "",
    fetcher,
    prefecture: str = "",
    city: str = "",
    address: str = "",
    phone: str = "",
    source_title: str = "",
    snippet_text: str = "",
    strict_is_identity: bool = False,
) -> VerificationOutcome:
    """candidateを公式ページで再確認する。ネットワークアクセスはfetcher経由のみ。"""
    if "@" not in candidate_email:
        return VerificationOutcome(status=STATUS_DOMAIN_MISMATCH, rationale="メール形式ではない")
    email_domain = candidate_email.rsplit("@", 1)[-1]

    prefetch = assess_candidate_source(
        clinic_name=clinic_name, prefecture=prefecture, city=city, address=address, phone=phone,
        candidate_email=candidate_email, source_url=candidate_source_url,
        source_title=source_title, snippet_text=snippet_text,
    )
    exceptional_source = prefetch.status != PREFETCH_DEFAULT and prefetch.allow_fetch
    official_host = site_host(known_hp_url) if known_hp_url else ""
    source_host = site_host(candidate_source_url) if candidate_source_url else ""
    source_on_known_official = bool(
        official_host and source_host and _is_same_or_subdomain(source_host, official_host)
    )
    if ((is_third_party_domain(email_domain) or is_third_party_domain(candidate_source_url))
            and not exceptional_source and not source_on_known_official):
        return VerificationOutcome(
            status=STATUS_REJECTED_THIRD_PARTY,
            rationale="求人/ポータル/Web制作会社等の第三者ドメインのため再確認自体を行わない",
        )

    if official_host:
        target_host = official_host
        if not source_on_known_official and not _is_same_or_subdomain(email_domain, official_host):
            return VerificationOutcome(
                status=STATUS_DOMAIN_MISMATCH,
                rationale=f"候補メールのdomain({email_domain})が公式HP({official_host})と不一致。"
                          "フリーメール/別domainはVERIFIEDにしない。",
            )
        fetch_url = candidate_source_url if source_on_known_official else known_hp_url
    elif exceptional_source:
        target_host = source_host
        fetch_url = candidate_source_url
    else:
        # 既知の公式HPが無いクリニック: 検索結果のURL自体が公式HPかどうか不明なため、
        # そのURLを「新規に見つかった公式HP候補」として、そのページ自体で確認する。
        if not source_host or not _is_same_or_subdomain(email_domain, source_host):
            return VerificationOutcome(
                status=STATUS_NO_OFFICIAL_REFERENCE,
                rationale="既知の公式HPが無く、候補メールのdomainと検索結果URLのdomainも不一致",
            )
        target_host = source_host
        fetch_url = candidate_source_url

    if not fetch_url:
        return VerificationOutcome(status=STATUS_UNCONFIRMED, rationale="再確認用のURLが無い")

    try:
        page = fetcher.fetch(fetch_url, allowed_host=target_host)
    except WebError as exc:
        return VerificationOutcome(status=STATUS_VERIFY_FETCH_FAILED, rationale=str(exc))

    if _is_content_page(page.url, page.title):
        return VerificationOutcome(
            status=STATUS_UNCONFIRMED,
            rationale="再取得先がblog/news等のcontent pageのため採用しない",
        )

    page_emails = dedupe_emails(_extract_non_article_emails(page.html))
    matched = next((e for e in page_emails if e.email.lower() == candidate_email.lower()), None)
    if matched is None:
        return VerificationOutcome(
            status=STATUS_UNCONFIRMED,
            rationale="公式ページ上で候補メールを再確認できなかった",
        )
    if (not exceptional_source and not source_on_known_official
            and not _email_domain_matches_site(matched.email, target_host)):
        return VerificationOutcome(
            status=STATUS_DOMAIN_MISMATCH,
            rationale="再取得ページ上の表記はあるが、メールdomainが公式host自身と一致しない(フリーメール等)",
        )
    if matched.extraction_method == "MAILTO" and _mailto_display_conflicts(matched.email, matched.context):
        return VerificationOutcome(
            status=STATUS_UNCONFIRMED,
            rationale="mailtoの表示文字列が別アドレスのため信用しない",
        )

    page_text_for_check = f"{page.title} {getattr(page, 'main_text', '')}"
    email_context = extract_email_context(page.html, candidate_email, page_title=page.title)

    if not official_host and not exceptional_source:
        # Official Site Trust Gate(known_hp_urlが無い「新規発見domain」分岐専用):
        # official_hp_email_crawler経由のOFFICIAL_HP crawlと同じく、見つけたdomain自体が
        # 複数医院を掲載する第三者ポータル(例: nihon-byouin.jp/bihokuikiiki.jp。
        # doctors-search.comと同型)である可能性がある。assess_official_trust()を再利用し
        # (判定ロジックを複製しない)、ポータル語彙・footer/copyright不一致まで含めて判定する。
        # page.text(nav/footer込み)を使う(main_textはfooterのcopyright表記を除去済みのため)。
        trust = assess_official_trust(
            hp_url=fetch_url, clinic_name=clinic_name, prefecture=prefecture,
            crawled_text_sample=getattr(page, "text", page_text_for_check),
        )
        if trust.status == _TRUST_NON_OFFICIAL:
            return VerificationOutcome(
                status=STATUS_REJECTED_THIRD_PARTY,
                official_domain=target_host,
                verified_page_url=page.url,
                rationale=f"Official Site Trust Gate(新規発見domain): {trust.rationale}",
            )
        if trust.status == _TRUST_REVIEW_OFFICIALITY:
            return VerificationOutcome(
                status=STATUS_REVIEW,
                official_domain=target_host,
                verified_page_url=page.url,
                evidence_text=matched.context or matched.raw_match,
                rationale=f"Official Site Trust Gate(新規発見domain): {trust.rationale}",
                nearby_text=email_context.nearby_text,
                email_context=email_context,
            )

    # Gate C (Email Ownership): Page Ownershipと分離し、メール周辺の見出し・段落・
    # footer/operator・組織名から、対象医院または同一法人の窓口かを判定する。
    ownership = assess_email_ownership(
        clinic_name=clinic_name, email_context=email_context, source_url=page.url,
    )
    if ownership.status == OWNERSHIP_MISMATCH:
        return VerificationOutcome(
            status=STATUS_REJECTED_THIRD_PARTY,
            official_domain=target_host,
            verified_page_url=page.url,
            rationale=f"Email Ownership Gate: {ownership.reason}",
            nearby_text=email_context.nearby_text,
            email_context=email_context,
        )

    # Gate A (Clinic Identity): 所在地情報があるのに別の都道府県が明記されている場合は所在地相違の疑い
    # (FP-1相当: 同名/類似名クリニックが別の都道府県に実在するケースを防ぐ)。
    if _location_contradiction(page_text_for_check, prefecture):
        return VerificationOutcome(
            status=STATUS_REVIEW,
            official_domain=target_host,
            verified_page_url=page.url,
            evidence_text=matched.context or matched.raw_match,
            rationale="domain一致はしたが、ページ内に異なる都道府県名が明記されており所在地相違の疑いがあるため人手確認",
            nearby_text=email_context.nearby_text,
            email_context=email_context,
        )

    # Gate A (Clinic Identity): 医院名(正式名称の核)の完全一致を必須とする(名称一部一致だけでは
    # 昇格させない。FP-1はこの厳格化だけでも防げるケース)。
    if not _core_name_present(page_text_for_check, clinic_name):
        return VerificationOutcome(
            status=STATUS_REVIEW,
            official_domain=target_host,
            verified_page_url=page.url,
            evidence_text=matched.context or matched.raw_match,
            rationale="domain一致はしたが、ページ内に想定クリニック名(正式名称)が確認できず別施設の疑いがあるため人手確認",
            nearby_text=email_context.nearby_text,
            email_context=email_context,
        )

    if strict_is_identity:
        compact_page = re.sub(r"\s+", "", unicodedata.normalize("NFKC", page_text_for_check or ""))
        missing = [value for value in (prefecture, city)
                   if value and re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)) not in compact_page]
        if missing:
            return VerificationOutcome(
                status=STATUS_REVIEW, official_domain=target_host, verified_page_url=page.url,
                evidence_text=matched.context or matched.raw_match,
                rationale="IS Clinic Identity Gate: 医院名は一致したが所在地を確認できない: "
                          + ", ".join(missing),
                nearby_text=email_context.nearby_text, email_context=email_context,
            )

    if prefetch.status == STATUS_TRUSTED_PUBLIC_MEDICAL_DETAIL and not _address_match(address, page_text_for_check):
        return VerificationOutcome(
            status=STATUS_REVIEW, official_domain=target_host, verified_page_url=page.url,
            evidence_text=matched.context or matched.raw_match,
            rationale="Trusted Public Medical Detailだがfetch後ページで対象住所を確認できない",
            nearby_text=email_context.nearby_text, email_context=email_context,
        )

    if ownership.status != OWNERSHIP_MATCH:
        return VerificationOutcome(
            status=STATUS_REVIEW, official_domain=target_host, verified_page_url=page.url,
            evidence_text=matched.context or matched.raw_match,
            rationale=f"Email Ownership Gate: {ownership.reason}",
            nearby_text=email_context.nearby_text, email_context=email_context,
        )

    return VerificationOutcome(
        status=STATUS_VERIFIED,
        verified_on_official=True,
        official_domain=target_host,
        verified_page_url=page.url,
        evidence_text=f"{matched.context or matched.raw_match} ({matched.extraction_method} at {page.url})",
        rationale="Page OwnershipとEmail Ownershipの両方を確認。"
                  f"Gate C: {ownership.reason}. Pre-fetch: {prefetch.reason or '通常公式domain'}",
        nearby_text=email_context.nearby_text,
        email_context=email_context,
    )
