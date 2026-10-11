"""Email Enrichmentの各経路(公式HP crawl / Web検索 / AI Overview)を統合し、
6種類の正式status(VERIFIED_EMAIL/SEARCH_CANDIDATE/FORM_ONLY/REVIEW/NO_EMAIL/FETCH_FAILED)
のどれかに正規化するだけのオーケストレーター。

取得優先順位(ユーザー要件):
  1. 公式HP mailto / 2. 公式HP本文 / 3. 公式お問い合わせページ / 4. 公式法人ページ
     -> official_hp_email_crawler(既存エンジン、変更しない)
  5. Web検索結果 -> search_candidate_adapter(新規、candidate discoveryのみ)
  6. AI Overview / AI推奨 / snippet -> ai_overview_adapter(新規、隔離、candidate discoveryのみ)

5/6で見つかった候補は、必ずcandidate_verifierで公式ページ再確認してからでないと
VERIFIED_EMAILにしない。公式crawl(1-4)で既にVERIFIED_EMAILが得られている場合は、
検索APIの消費を避けるため5/6はスキップする。

検索/AI Overviewの失敗(SEARCH_FAILED/ERROR)は、それ単体でNO_EMAILを意味しない
(= 検索に失敗しただけで「メールが無い」と結論しない)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
import unicodedata

from src.enrichment.email_research import official_hp_email_crawler as official_crawler
from src.enrichment.email_research.ai_overview_adapter import (
    AIOverviewAdapter,
    extract_candidate_emails_from_ai_overview,
)
from src.enrichment.email_research.candidate_verifier import (
    STATUS_REVIEW,
    STATUS_VERIFIED,
    verify_candidate,
)
from src.enrichment.email_research.clinic_identity import OWNERSHIP_MATCH, assess_email_ownership
from src.enrichment.email_research.clinic_identity import _core_name_present
from src.enrichment.email_research.official_site_trust import (
    STATUS_OFFICIAL_CONFIRMED,
    STATUS_REVIEW_OFFICIALITY,
    assess_official_trust,
)
from src.enrichment.email_research.search_candidate_adapter import (
    QUERY_TEMPLATES,
    extract_candidate_emails_from_results,
    location_suffix_text,
)
from src.enrichment.email_research.email_role_classifier import classify_email_role
from src.enrichment.search_provider import SearchError

PIPELINE_VERSION = "clinic-email-pipeline-0.1.0"

STATUS_VERIFIED_EMAIL = "VERIFIED_EMAIL"
STATUS_SEARCH_CANDIDATE = "SEARCH_CANDIDATE"
STATUS_FORM_ONLY = "FORM_ONLY"
STATUS_REVIEW_FINAL = "REVIEW"
STATUS_NO_EMAIL = "NO_EMAIL"
STATUS_FETCH_FAILED = "FETCH_FAILED"

SOURCE_OFFICIAL_HP = "OFFICIAL_HP"
SOURCE_SEARCH_QUERY = "SEARCH_QUERY"
SOURCE_AI_OVERVIEW = "AI_OVERVIEW"

_CONFIDENCE_BY_STATUS = {
    STATUS_VERIFIED_EMAIL: 1.0,
    STATUS_REVIEW_FINAL: 0.5,
    STATUS_SEARCH_CANDIDATE: 0.3,
}


@dataclass(frozen=True)
class EmailResultRow:
    clinic_id: str
    email: str
    status: str  # VERIFIED_EMAIL | SEARCH_CANDIDATE | REVIEW
    source_type: str  # OFFICIAL_HP | SEARCH_QUERY | AI_OVERVIEW
    source_url: str
    source_query: str
    official_domain: str
    verified_on_official: bool
    confidence: float
    evidence_text: str
    checked_at: str
    parser_version: str
    third_party_suspected: bool = False
    rationale: str = ""
    nearby_text: str = ""  # Hardening Phase 2: Role Classification用の周辺文脈(追加専用field)
    email_context: object = None  # Fresh Canary v2: email_context_extractor.EmailContext(追加専用)
    verifier_raw_status: str = ""  # Fresh Canary v3: candidate_verifierの生status(pre-fetch/fetch
                                     # stage分析用。DOMAIN_MISMATCH/NO_OFFICIAL_REFERENCE/
                                     # UNCONFIRMED_ON_OFFICIAL_PAGE/VERIFY_FETCH_FAILED/VERIFIED/REVIEW。
                                     # OFFICIAL_HP経路では""(verify_candidateを経由しないため)。

    def to_sidecar_row(self) -> dict:
        """Legacy artifact compatibility shape; formal runtime persistence uses its adapter."""
        return {
            "clinic_id": self.clinic_id,
            "email": self.email,
            "status": self.status,
            "source_type": self.source_type,
            "source_url": self.source_url,
            "source_query": self.source_query,
            "official_domain": self.official_domain,
            "verified_on_official": self.verified_on_official,
            "confidence": self.confidence,
            "evidence_text": self.evidence_text,
            "checked_at": self.checked_at,
            "parser_version": self.parser_version,
        }


@dataclass(frozen=True)
class RejectedCandidateRow:
    """third-partyとして即棄却された候補の詳細(Human Audit用。追加専用フィールドであり、
    既存のrejected_third_party_countの意味・既存呼び出し元の挙動は一切変更しない)。"""
    clinic_id: str
    email: str
    source_type: str
    source_url: str
    source_query: str
    rationale: str
    checked_at: str


@dataclass
class PipelineResult:
    clinic_id: str
    clinic_name: str
    email_rows: list[EmailResultRow] = field(default_factory=list)
    forms_found: bool = False
    official_crawl_status: str = ""  # official_hp_email_crawlerの元status(未実行なら"")
    search_status: str = ""  # SearchCandidateAdapterの元status(未実行なら"")
    ai_overview_status: str = ""  # AIOverviewAdapterの元status(未実行なら"")
    rejected_third_party_count: int = 0
    rejected_rows: list[RejectedCandidateRow] = field(default_factory=list)
    official_trust_status: str = ""  # OFFICIAL_CONFIRMED | NON_OFFICIAL | REVIEW_OFFICIALITY | ""(未実行)
    notes: list[str] = field(default_factory=list)
    pipeline_version: str = PIPELINE_VERSION
    clinic_status: str = ""  # 6種類のいずれか(run_pipeline側で解決してから設定する)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_identity_present(text: str, clinic_name: str, prefecture: str, city: str) -> bool:
    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))
    return _core_name_present(text, clinic_name) and all(
        not value or re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)) in compact
        for value in (prefecture, city)
    )


def _verify_and_build_row(
    *,
    clinic_id: str,
    clinic_name: str,
    email: str,
    source_url: str,
    source_query: str,
    source_type: str,
    known_hp_url: str,
    third_party_suspected: bool,
    fetcher,
    checked_at: str,
    prefecture: str = "",
    city: str = "",
    address: str = "",
    phone: str = "",
    source_title: str = "", strict_is_identity: bool = False,
    snippet_text: str = "",
) -> tuple[EmailResultRow | None, RejectedCandidateRow | None]:
    """1候補を再確認し、採用可否とrow(無ければNone)を返す。third-party即棄却なら
    RejectedCandidateRow(Human Audit用の詳細)を返す。"""
    outcome = verify_candidate(
        email, clinic_name, known_hp_url=known_hp_url, candidate_source_url=source_url, fetcher=fetcher,
        prefecture=prefecture, city=city, address=address, phone=phone,
        source_title=source_title, snippet_text=snippet_text,
        strict_is_identity=strict_is_identity,
    )
    if outcome.status == "REJECTED_THIRD_PARTY":
        rejected = RejectedCandidateRow(
            clinic_id=clinic_id, email=email, source_type=source_type, source_url=source_url,
            source_query=source_query, rationale=outcome.rationale, checked_at=checked_at,
        )
        return None, rejected

    if outcome.status == STATUS_VERIFIED:
        status = STATUS_VERIFIED_EMAIL
    elif outcome.status == STATUS_REVIEW:
        status = STATUS_REVIEW_FINAL
    else:
        status = STATUS_SEARCH_CANDIDATE

    row = EmailResultRow(
        clinic_id=clinic_id, email=email, status=status, source_type=source_type,
        source_url=outcome.verified_page_url or source_url, source_query=source_query,
        official_domain=outcome.official_domain, verified_on_official=outcome.verified_on_official,
        confidence=_CONFIDENCE_BY_STATUS[status], evidence_text=outcome.evidence_text,
        checked_at=checked_at, parser_version=f"{PIPELINE_VERSION}+{outcome.verifier_version}",
        third_party_suspected=third_party_suspected, rationale=outcome.rationale,
        nearby_text=outcome.nearby_text, email_context=outcome.email_context,
        verifier_raw_status=outcome.status,
    )
    return row, None


def run_pipeline(
    *,
    clinic_id: str,
    clinic_name: str,
    hp_url: str = "",
    official_crawl_eligible: bool = False,
    prefecture: str = "",
    city: str = "",
    address: str = "",
    phone: str = "",
    search_provider=None,
    fetcher,
    max_search_queries: int = 6,
    run_ai_overview: bool = True,
    fast_acquisition_mode: bool = False,
) -> PipelineResult:
    checked_at = _now_iso()
    result = PipelineResult(clinic_id=clinic_id, clinic_name=clinic_name)

    known_hp_url = hp_url if (official_crawl_eligible and hp_url) else ""
    official_hard_failure = False

    if known_hp_url:
        crawl_result = official_crawler.crawl_clinic(clinic_id, clinic_name, known_hp_url, fetcher=fetcher)
        result.official_crawl_status = crawl_result.research_status
        if crawl_result.research_status == "FOUND":
            # Official Website Trust Gate(Hardening Phase 2, Problem A対応): hp_status='VERIFIED'
            # という production DBの事前確認だけに頼らず、実際に取得したページ内容自体が
            # 「対象医院自身の公式サイト」なのか「複数医院を掲載する第三者ポータル」なのかを
            # 確認する。実例: clinic_id=541 杉野内科クリニックのhp_urlがdoctors-search.com
            # (ポータル)を指し、Trust Gateが無いとwebmaster@doctors-search.comがVERIFIED_EMAIL
            # まで到達してしまっていた。
            trust = assess_official_trust(
                hp_url=known_hp_url, clinic_name=clinic_name, prefecture=prefecture,
                crawled_text_sample=crawl_result.crawled_text_sample,
            )
            result.official_trust_status = trust.status
            if trust.status == STATUS_OFFICIAL_CONFIRMED:
                strict_official_identity = (not fast_acquisition_mode) or _is_identity_present(
                    crawl_result.crawled_text_sample, clinic_name, prefecture, city
                )
                for e in crawl_result.emails:
                    # Part 2(OFFICIAL_HP上のEmail Ownership): サイト全体はOFFICIAL_CONFIRMEDでも、
                    # 個々のメール周辺の文脈が学会/財団/協会等の第三者組織を示す場合は、
                    # そのメールだけをthird-partyとして除外する(風の木クリニックの8メールは
                    # いずれもこの文脈に該当しないため、既知True Positiveは保持される)。
                    ownership = assess_email_ownership(
                        clinic_name=clinic_name, email_context=e.email_context, source_url=e.page_url,
                    )
                    if ownership.status != OWNERSHIP_MATCH:
                        result.rejected_rows.append(RejectedCandidateRow(
                            clinic_id=clinic_id, email=e.email, source_type=SOURCE_OFFICIAL_HP,
                            source_url=e.page_url, source_query="",
                            rationale=f"OFFICIAL_HP Email Ownership Gate: {ownership.reason}",
                            checked_at=checked_at,
                        ))
                        result.rejected_third_party_count += 1
                        continue
                    official_status = STATUS_VERIFIED_EMAIL if strict_official_identity else STATUS_REVIEW_FINAL
                    result.email_rows.append(EmailResultRow(
                        clinic_id=clinic_id, email=e.email, status=official_status,
                        source_type=SOURCE_OFFICIAL_HP, source_url=e.page_url, source_query="",
                        official_domain=official_crawler.host(known_hp_url), verified_on_official=True,
                        confidence=_CONFIDENCE_BY_STATUS[official_status], evidence_text=e.evidence_text, checked_at=checked_at,
                        parser_version=f"{PIPELINE_VERSION}+{official_crawler.ENGINE_VERSION}",
                        nearby_text=e.nearby_text, email_context=e.email_context,
                        rationale="" if strict_official_identity else
                                  "IS Clinic Identity Gate: 公式ページで医院名・都道府県・市区町村を全て確認できない",
                    ))
            elif trust.status == STATUS_REVIEW_OFFICIALITY:
                # 公式性を確証できないため、VERIFIED_EMAILへは上げずREVIEWとして人手確認に回す
                # (Conservative Promotion)。known_hp_urlは以降の検索フォールバックでも
                # 「確証された公式domain」として使わない(Gate Bへ誤った基準を渡さないため)。
                for e in crawl_result.emails:
                    result.email_rows.append(EmailResultRow(
                        clinic_id=clinic_id, email=e.email, status=STATUS_REVIEW_FINAL,
                        source_type=SOURCE_OFFICIAL_HP, source_url=e.page_url, source_query="",
                        official_domain=official_crawler.host(known_hp_url), verified_on_official=False,
                        confidence=_CONFIDENCE_BY_STATUS[STATUS_REVIEW_FINAL], evidence_text=e.evidence_text,
                        checked_at=checked_at, parser_version=f"{PIPELINE_VERSION}+{official_crawler.ENGINE_VERSION}",
                        rationale=trust.rationale, nearby_text=e.nearby_text, email_context=e.email_context,
                    ))
                known_hp_url = ""
            else:  # STATUS_NON_OFFICIAL
                for e in crawl_result.emails:
                    result.rejected_rows.append(RejectedCandidateRow(
                        clinic_id=clinic_id, email=e.email, source_type=SOURCE_OFFICIAL_HP,
                        source_url=e.page_url, source_query="", rationale=trust.rationale,
                        checked_at=checked_at,
                    ))
                    result.rejected_third_party_count += 1
                known_hp_url = ""
        elif crawl_result.research_status == "FORM_ONLY":
            result.forms_found = True
        elif crawl_result.research_status == "FETCH_FAILED":
            official_hard_failure = True

    def _sales_usable_verified(row: EmailResultRow) -> bool:
        if row.status != STATUS_VERIFIED_EMAIL:
            return False
        ctx = row.email_context
        role = classify_email_role(
            row.email,
            section_heading=getattr(ctx, "section_heading", ""),
            nearby_text=getattr(ctx, "nearby_text", "") or row.nearby_text,
            anchor_text=getattr(ctx, "anchor_text", ""),
            parent_text=getattr(ctx, "parent_text", ""),
            page_url=row.source_url,
            page_title=getattr(ctx, "page_title", ""),
        )
        return role.role in {"GENERAL", "OFFICE"}

    already_verified = any(
        _sales_usable_verified(r) if fast_acquisition_mode else r.status == STATUS_VERIFIED_EMAIL
        for r in result.email_rows
    )

    if not already_verified and search_provider is not None:
        phase = _run_is_search_phase if fast_acquisition_mode else _run_search_and_ai_overview_phase
        phase(
            result=result, clinic_id=clinic_id, clinic_name=clinic_name, prefecture=prefecture,
            city=city, address=address, phone=phone, known_hp_url=known_hp_url, search_provider=search_provider,
            fetcher=fetcher, checked_at=checked_at, max_search_queries=max_search_queries,
            run_ai_overview=run_ai_overview,
        )

    result.clinic_status = _resolve_clinic_status(result, official_hard_failure)
    return result


def _run_is_search_phase(
    *, result: "PipelineResult", clinic_id, clinic_name, prefecture, city, address, phone, known_hp_url,
    search_provider, fetcher, checked_at, max_search_queries, run_ai_overview,
) -> None:
    """正式IS運用: 1医院1query。organic/snippetと同一responseのAI Overviewを使う。"""
    del max_search_queries  # IS modeは常に1 request。
    query = " ".join(p for p in (clinic_name, "メール", prefecture, city) if p)
    try:
        if hasattr(search_provider, "search_raw"):
            raw = search_provider.search_raw(query)
            organic = raw.get("organic_results", []) if isinstance(raw, dict) else []
            ai_payload = raw.get("ai_overview") if isinstance(raw, dict) else None
        else:
            organic = search_provider.search(query)
            ai_payload = None
    except SearchError:
        result.search_status = "SEARCH_FAILED"
        result.ai_overview_status = "ERROR" if run_ai_overview else ""
        return

    organic_candidates = extract_candidate_emails_from_results(organic, query)
    result.search_status = "OK" if organic_candidates else "NO_RESULTS"
    _apply_candidates(
        result=result, candidates=organic_candidates, clinic_id=clinic_id, clinic_name=clinic_name,
        source_type=SOURCE_SEARCH_QUERY, source_query_fallback=query, known_hp_url=known_hp_url,
        fetcher=fetcher, checked_at=checked_at, prefecture=prefecture, city=city,
        address=address, phone=phone, strict_is_identity=True,
    )
    if run_ai_overview:
        ai_candidates = extract_candidate_emails_from_ai_overview(ai_payload, query)
        result.ai_overview_status = "PRESENT" if ai_candidates else "ABSENT"
        _apply_candidates(
            result=result, candidates=ai_candidates, clinic_id=clinic_id, clinic_name=clinic_name,
            source_type=SOURCE_AI_OVERVIEW, source_query_fallback=query, known_hp_url=known_hp_url,
            fetcher=fetcher, checked_at=checked_at, prefecture=prefecture, city=city,
            address=address, phone=phone, strict_is_identity=True,
        )


def _apply_candidates(
    *, result: "PipelineResult", candidates, clinic_id, clinic_name, source_type, source_query_fallback,
    known_hp_url, fetcher, checked_at, prefecture="", city="", address="", phone="",
    strict_is_identity=False,
) -> bool:
    """candidate群をverify_candidateで検証し、resultへ反映する。VERIFIED_EMAILが1件でも
    得られたらTrueを返す(呼び出し側の早期終了判定に使う)。"""
    verified_found = False
    for candidate in candidates:
        row, rejected_row = _verify_and_build_row(
            clinic_id=clinic_id, clinic_name=clinic_name, email=candidate.email,
            source_url=getattr(candidate, "source_url", ""),
            source_query=getattr(candidate, "matched_query", source_query_fallback),
            source_type=source_type, known_hp_url=known_hp_url,
            third_party_suspected=candidate.third_party_suspected, fetcher=fetcher, checked_at=checked_at,
            prefecture=prefecture, city=city, address=address, phone=phone,
            source_title=getattr(candidate, "source_title", ""),
            snippet_text=getattr(candidate, "snippet_text", ""),
            strict_is_identity=strict_is_identity,
        )
        if rejected_row is not None:
            result.rejected_third_party_count += 1
            result.rejected_rows.append(rejected_row)
        elif row is not None:
            result.email_rows.append(row)
            if row.status == STATUS_VERIFIED_EMAIL:
                verified_found = True
    return verified_found


def _run_search_and_ai_overview_phase(
    *, result: "PipelineResult", clinic_id, clinic_name, prefecture, city, address, phone, known_hp_url,
    search_provider, fetcher, checked_at, max_search_queries, run_ai_overview,
) -> None:
    """コスト最小化のため、段階的にqueryを発行する。

    1. テンプレート(メール -> メールアドレス -> お問い合わせ)の順に、まずplain queryだけを試す。
    2. いずれかのqueryでVERIFIED_EMAILが得られたら、即座に残りのquery/AI Overviewを一切発行しない。
    3. plain queryが0件のときだけ、同名クリニックの誤認防止のため(prefecture/city付きの)
       disambiguation queryを同テンプレートで1回だけ追加発行する。
    4. AI Overviewは「organic検索で候補が得られない、または補強が必要」(= 最後までVERIFIEDに
       到達できなかった)場合だけ使う。最初のplain query(1件目)をsearch_raw()で発行できた場合は、
       そこに同梱されたai_overviewを再利用し、追加APIコールを発生させない。それが無い場合に限り、
       最後の手段として1回だけ専用のAI Overview問い合わせを行う。
    """
    location_suffix = location_suffix_text(prefecture, city)
    queries_issued = 0
    any_candidates_extracted = False
    any_error = False
    first_call_ai_overview_payload = None
    first_call_query = ""
    first_call_capture_attempted = False  # search_rawで1件目を発行できた(例外にならなかった)かどうか

    def _normalize_raw_organic(raw_payload):
        organic = raw_payload.get("organic_results") if isinstance(raw_payload, dict) else None
        return organic if isinstance(organic, list) else []

    def _issue(query: str, *, capture_ai_overview: bool) -> list:
        nonlocal queries_issued, any_error, any_candidates_extracted
        nonlocal first_call_ai_overview_payload, first_call_query, first_call_capture_attempted
        if queries_issued >= max_search_queries:
            return []
        queries_issued += 1
        try:
            if capture_ai_overview and hasattr(search_provider, "search_raw"):
                raw_payload = search_provider.search_raw(query)
                first_call_ai_overview_payload = (
                    raw_payload.get("ai_overview") if isinstance(raw_payload, dict) else None
                )
                first_call_query = query
                first_call_capture_attempted = True
                raw_results = _normalize_raw_organic(raw_payload)
            else:
                raw_results = search_provider.search(query)
        except SearchError:
            any_error = True
            return []
        candidates = extract_candidate_emails_from_results(raw_results, query)
        if candidates:
            any_candidates_extracted = True
        return candidates

    verified_found = False
    for index, template in enumerate(QUERY_TEMPLATES):
        if verified_found or queries_issued >= max_search_queries:
            break
        plain_query = f"{clinic_name} {template}"
        plain_candidates = _issue(plain_query, capture_ai_overview=(index == 0))
        verified_found = _apply_candidates(
            result=result, candidates=plain_candidates, clinic_id=clinic_id, clinic_name=clinic_name,
            source_type=SOURCE_SEARCH_QUERY, source_query_fallback=plain_query, known_hp_url=known_hp_url,
            fetcher=fetcher, checked_at=checked_at, prefecture=prefecture, city=city, address=address, phone=phone,
        ) or verified_found
        if verified_found:
            break
        if location_suffix and not plain_candidates and queries_issued < max_search_queries:
            disamb_query = f"{clinic_name} {location_suffix} {template}"
            disamb_candidates = _issue(disamb_query, capture_ai_overview=False)
            verified_found = _apply_candidates(
                result=result, candidates=disamb_candidates, clinic_id=clinic_id, clinic_name=clinic_name,
                source_type=SOURCE_SEARCH_QUERY, source_query_fallback=disamb_query, known_hp_url=known_hp_url,
                fetcher=fetcher, checked_at=checked_at, prefecture=prefecture, city=city, address=address, phone=phone,
            ) or verified_found

    if any_error and not any_candidates_extracted:
        result.search_status = "SEARCH_FAILED"
    elif any_candidates_extracted:
        result.search_status = "OK"
    else:
        result.search_status = "NO_RESULTS"

    if verified_found or not run_ai_overview:
        return

    if first_call_capture_attempted:
        # 1件目のsearch_rawで既にai_overviewの有無を確認済み。無ければ正常系(ABSENT)として
        # 追加のAPIコールは発生させない(payload自体がNone/absentでも二重発行しない)。
        ai_candidates = extract_candidate_emails_from_ai_overview(first_call_ai_overview_payload, first_call_query)
        result.ai_overview_status = "PRESENT" if ai_candidates else "ABSENT"
        _apply_candidates(
            result=result, candidates=ai_candidates, clinic_id=clinic_id, clinic_name=clinic_name,
            source_type=SOURCE_AI_OVERVIEW, source_query_fallback=first_call_query, known_hp_url=known_hp_url,
            fetcher=fetcher, checked_at=checked_at, prefecture=prefecture, city=city, address=address, phone=phone,
        )
    elif not hasattr(search_provider, "search_raw"):
        result.ai_overview_status = "ABSENT"
    else:
        # 1件目をsearch_rawで発行できなかった(例外/budget切れ)場合のみ、最後の手段として
        # 専用のAI Overview問い合わせを1回だけ行う。
        ai_adapter = AIOverviewAdapter(search_provider)
        base_query = f"{clinic_name} {QUERY_TEMPLATES[0]}"
        ai_result = ai_adapter.run(base_query)
        result.ai_overview_status = ai_result.status
        _apply_candidates(
            result=result, candidates=ai_result.candidates, clinic_id=clinic_id, clinic_name=clinic_name,
            source_type=SOURCE_AI_OVERVIEW, source_query_fallback=base_query, known_hp_url=known_hp_url,
            fetcher=fetcher, checked_at=checked_at, prefecture=prefecture, city=city, address=address, phone=phone,
        )


def _resolve_clinic_status(result: PipelineResult, official_hard_failure: bool) -> str:
    if any(r.status == STATUS_VERIFIED_EMAIL for r in result.email_rows):
        return STATUS_VERIFIED_EMAIL
    if any(r.status == STATUS_REVIEW_FINAL for r in result.email_rows):
        return STATUS_REVIEW_FINAL
    if any(r.status == STATUS_SEARCH_CANDIDATE for r in result.email_rows):
        return STATUS_SEARCH_CANDIDATE

    search_failed = result.search_status == "SEARCH_FAILED"
    ai_errored = result.ai_overview_status == "ERROR"
    if official_hard_failure or search_failed or ai_errored:
        result.notes.append("検索/取得に失敗した経路がありNO_EMAILと断定できないため、FETCH_FAILEDとして扱う")
        return STATUS_FETCH_FAILED

    if result.forms_found:
        return STATUS_FORM_ONLY

    return STATUS_NO_EMAIL
