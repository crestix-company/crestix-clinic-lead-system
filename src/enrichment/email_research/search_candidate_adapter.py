"""Web検索(SerpAPI)をcandidate discoveryとして使うAdapter(Verified判定は一切行わない)。

設計方針:
- 「クリニック名 + メール」「クリニック名 + メールアドレス」「クリニック名 + お問い合わせ」の
  3テンプレートで検索する。同名クリニックの誤認を防ぐため、都道府県/市区町村/住所を
  必要に応じてqueryへ追加する(build_queries)。
- 検索結果(organic_results.title/link/snippet)から、文字列として明示的に存在する
  メールだけを抽出する(推測によるメール生成は一切しない)。
- ここで見つかった候補は常にSEARCH_CANDIDATE相当であり、VERIFIED_EMAILには一切しない。
  VERIFIED判定はsrc.enrichment.email_research.candidate_verifierの責務。
- 既存のofficial_hp_email_crawler/email_extractionは変更しない(車輪の再発明をしない:
  メール妥当性判定はemail_extraction.is_valid_email/normalize_email_textを再利用し、
  第三者サイト判定はhp_analysis.NON_OFFICIALを再利用する)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from src.enrichment.email_research.email_extraction import is_valid_email, normalize_email_text
from src.enrichment.hp_analysis import NON_OFFICIAL, host as site_host
from src.enrichment.search_provider import SearchError
from src.master.email_sales_eligibility import source_owner_type

ADAPTER_VERSION = "search-candidate-adapter-0.1.0"

QUERY_TEMPLATES: tuple[str, ...] = ("メール", "メールアドレス", "お問い合わせ")

# NON_OFFICIAL(hp_analysis)に無い、求人/ポータル/Web制作会社系の追加ブロックリスト。
# これらのdomainから見つかった候補は「第三者サイト」としてcandidateに残すが、
# candidate_verifier側で絶対にVERIFIEDへ昇格させない(third-party FP=0のための保険)。
EXTRA_THIRD_PARTY_DOMAINS: frozenset[str] = frozenset({
    "indeed.com", "en-gage.net", "doda.jp", "type.jp", "baitoru.com", "rikunabi.com",
    "rikunabi-next.jp", "hatarako.net", "wantedly.com", "hellowork.mhlw.go.jp",
    "求人ボックス.com", "kyujinbox.com", "townwork.net", "froma.jp", "airwork.net",
    "jmty.jp", "minano-kaigo.com", "お仕事なび.com",
    # 無料ホームページ作成サービスの代表的ドメイン(Web制作会社の制作デモ/テンプレ跡地に多い)
    "wixsite.com", "jimdofree.com", "jimdo.com", "amebaownd.com", "goope.jp",
    "crayonsite.net", "crayonsite.com", "grupo.jp", "ocnk.net",
    # Hardening Phase 2: 医院検索/医師検索ポータル(Fresh 100-clinic Canary clinic_id=541
    # 杉野内科クリニックの実例。hp_url自体がこのポータルを指し、third-party FPとなった)。
    "doctors-search.com",
})

# Hardening Phase: 地方自治体(.lg.jp)・中央省庁(.go.jp)・大学/学校(.ac.jp/.ed.jp)は、
# 民間クリニックが自ら保有するドメインではない(100医院CanaryのHuman Auditで、
# pref.mie.lg.jp/city.izumo.shimane.jp/city.takahama.lg.jp/yamagata-u.ac.jp等が
# REVIEW止まりに回っていたことから、fetch前に third-party として確定できる安全な追加ルール)。
# .or.jpは医療法人自身が保有するケースもあるため、ここには含めない
# (ownershipの判定はcandidate_verifierのGate C=第三者組織キーワード検出に委ねる)。
_GOV_ACADEMIC_TLD_SUFFIXES: tuple[str, ...] = (".lg.jp", ".go.jp", ".ac.jp", ".ed.jp")


def _registrable_host(url_or_email_domain: str) -> str:
    value = (url_or_email_domain or "").lower().strip().rstrip(".")
    return value[4:] if value.startswith("www.") else value


def is_third_party_domain(url_or_host: str) -> bool:
    """既存のNON_OFFICIAL(本人確認の文脈で使われる第三者ドメイン集合)を再利用し、
    求人/ポータル/Web制作会社系ドメインを追加判定する。"""
    h = site_host(url_or_host) if "://" in (url_or_host or "") else _registrable_host(url_or_host)
    if not h:
        return False
    if any(h.endswith(suffix) for suffix in _GOV_ACADEMIC_TLD_SUFFIXES):
        return True
    all_blocked = NON_OFFICIAL | EXTRA_THIRD_PARTY_DOMAINS
    return any(h == d or h.endswith("." + d) for d in all_blocked)


def location_suffix_text(prefecture: str = "", city: str = "") -> str:
    """都道府県+市区町村の連結文字列(例: "東京都渋谷区")。pipeline.pyの段階的query構築でも使う。"""
    parts = [p.strip() for p in (prefecture, city) if p and p.strip()]
    return "".join(parts)


def build_queries(
    clinic_name: str,
    prefecture: str = "",
    city: str = "",
    address: str = "",
    *,
    include_disambiguation: bool = True,
) -> list[str]:
    """3テンプレートのplain query + (情報があれば)disambiguation付きqueryを返す。

    同名クリニックの誤認防止のため、都道府県/市区町村/住所を付与したqueryを
    追加するが、plain queryも必ず残す(disambiguation文字列が公開情報に無く
    ヒットしない場合のfallbackとして)。重複は除去する。
    """
    name = (clinic_name or "").strip()
    if not name:
        return []

    location_suffix = location_suffix_text(prefecture, city)

    queries: list[str] = []
    for template in QUERY_TEMPLATES:
        queries.append(f"{name} {template}")
    if include_disambiguation and location_suffix:
        for template in QUERY_TEMPLATES:
            queries.append(f"{name} {location_suffix} {template}")
    if include_disambiguation and address and address.strip() and address.strip() != location_suffix:
        # 住所まで必要になるのは、prefecture/cityだけでは絞り込めない同名クリニックが
        # 疑われる稀なケース想定。queryの肥大化を避けるため、まず1テンプレートだけ使う。
        queries.append(f"{name} {address.strip()} {QUERY_TEMPLATES[0]}")

    seen: set[str] = set()
    deduped: list[str] = []
    for q in queries:
        if q not in seen:
            seen.add(q)
            deduped.append(q)
    return deduped


@dataclass(frozen=True)
class SearchEmailCandidate:
    email: str
    source_url: str
    source_title: str
    snippet_text: str
    matched_query: str
    field_matched: str  # TITLE | SNIPPET | URL
    third_party_suspected: bool
    origin: str = "SEARCH_RESULT"
    source_owner_type: str = "UNKNOWN"
    # Discovery alone cannot establish the mailbox owner.
    email_ownership: str = "OWNERSHIP_UNKNOWN"


def _candidates_from_field(text: str, field_name: str) -> list[str]:
    from src.enrichment.email_research.email_extraction import _candidates_from_text  # pure regex scan
    return [c for c in _candidates_from_text(text or "") if is_valid_email(normalize_email_text(c))]


def extract_candidate_emails_from_results(
    results: list[dict], matched_query: str
) -> list[SearchEmailCandidate]:
    """organic_results(url/title/content)から、明示的に存在するメールだけを抽出する。

    推測によるメール生成(info@/contact@等の合成)は一切行わない。各結果のtitle/content(snippet)/
    urlそれぞれに対して正規表現スキャンするだけで、どのフィールドにも文字列として存在しない
    メールは候補にならない。
    """
    candidates: list[SearchEmailCandidate] = []
    seen: set[str] = set()
    for result in results or []:
        if not isinstance(result, dict):
            continue
        url = str(result.get("url", "") or result.get("link", "") or "")
        title = str(result.get("title", "") or "")
        snippet = str(result.get("content", "") or result.get("snippet", "") or "")

        for field_name, text in (("TITLE", title), ("SNIPPET", snippet), ("URL", url)):
            for raw_email in _candidates_from_field(text, field_name):
                normalized = normalize_email_text(raw_email)
                key = normalized.lower()
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(SearchEmailCandidate(
                    email=normalized, source_url=url, source_title=title, snippet_text=snippet,
                    matched_query=matched_query, field_matched=field_name,
                    # Legacy flag now describes only the email domain. The page
                    # publisher is recorded independently and never promoted to
                    # an email-owner decision at discovery time.
                    third_party_suspected=is_third_party_domain(
                        normalized.rsplit("@", 1)[-1] if "@" in normalized else ""
                    ),
                    source_owner_type=source_owner_type(discovery_source="SEARCH_QUERY", source_url=url),
                    email_ownership="OWNERSHIP_UNKNOWN",
                ))
    return candidates


@dataclass
class SearchAdapterResult:
    status: str  # OK | NO_RESULTS | SEARCH_FAILED
    queries_used: list[str] = field(default_factory=list)
    candidates: list[SearchEmailCandidate] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    adapter_version: str = ADAPTER_VERSION


class SearchCandidateAdapter:
    """SearchProvider(Protocol準拠)をラップし、1医院分のcandidate discoveryを行う。"""

    def __init__(self, provider, *, max_queries: int = 3):
        self.provider = provider
        self.max_queries = max_queries

    def run(
        self,
        clinic_name: str,
        prefecture: str = "",
        city: str = "",
        address: str = "",
        *,
        include_disambiguation: bool = True,
    ) -> SearchAdapterResult:
        all_queries = build_queries(
            clinic_name, prefecture, city, address, include_disambiguation=include_disambiguation
        )
        queries = all_queries[: self.max_queries] if self.max_queries else all_queries

        used: list[str] = []
        candidates: list[SearchEmailCandidate] = []
        errors: list[str] = []
        for query in queries:
            used.append(query)
            try:
                results = self.provider.search(query)
            except SearchError as exc:
                errors.append(f"{query}: {exc}")
                continue
            candidates.extend(extract_candidate_emails_from_results(results, query))

        if errors and not candidates:
            status = "SEARCH_FAILED"
        elif candidates:
            status = "OK"
        else:
            status = "NO_RESULTS"
        return SearchAdapterResult(status=status, queries_used=used, candidates=candidates, errors=errors)
