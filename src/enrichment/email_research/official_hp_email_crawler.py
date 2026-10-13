"""本人確認済み公式HPからの公開メール取得Crawler(Pilot/Canary規模専用)。

設計(artifacts/clinic_email_phase0/email_crawler_design.md 準拠):
- clinics.hp_status='VERIFIED' AND hp_rank<>'UNKNOWN' の医院のhp_urlだけを起点にする
  (この判定はDB接続を要するため呼び出し側の責務。本モジュールはhp_url文字列だけを受け取る)
- 同一ホスト(登録ドメイン)中心、深度2、最大6ページ、timeout10秒
- 安全なHTTP取得は src.enrichment.safe_web.SafeFetcher を再利用する
  (DNS pinning/SSRF対策・robots.txt・リダイレクト制御は車輪の再発明をしない)
- メール抽出は src.enrichment.email_research.email_extraction を再利用する
- 推測によるメール生成は一切しない
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from src.enrichment.hp_analysis import canonical_page, host
from src.enrichment.safe_web import SafeFetcher, WebError
from src.enrichment.email_research.email_context_extractor import EmailContext, extract_email_context
from src.enrichment.email_research.email_extraction import (
    classify_email_type,
    dedupe_emails,
    detect_forms,
    extract_all_emails,
    looks_like_other_facility,
)

ENGINE_VERSION = "official-hp-email-crawler-0.3.0"

MAX_PAGES = 6
MAX_DEPTH = 2
DEFAULT_TIMEOUT_SECONDS = 10
CRAWLED_TEXT_SAMPLE_MAX_CHARS = 6000  # Official Website Trust Gate用集約テキストの上限

_CANDIDATE_PATH_HINTS = re.compile(
    r"contact|inquiry|about|clinic|access|reservation|yoyaku|"
    r"問い合わせ|問合せ|医院概要|クリニック概要|アクセス|予約",
    re.I,
)

# 記事ページは第三者の連絡先や執筆者プロフィールを含みやすい。precisionを優先し、
# この種のページからはメールを採用しない（リンク探索自体は継続する）。
_CONTENT_PATH_RE = re.compile(r"(?:^|/)(?:blog|news|column|archive)(?:/|$)", re.I)
_CONTENT_TITLE_RE = re.compile(r"ブログ|ニュース|コラム|記事一覧|医師(?:紹介|経歴)|院長経歴", re.I)


def _normalized_site_host(value: str) -> str:
    value = (value or "").lower().strip().rstrip(".")
    return value[4:] if value.startswith("www.") else value


def _email_domain_matches_site(email: str, origin_host: str) -> bool:
    """メールdomainが公式HP host自身（またはその配下）である場合だけ採用する。

    法人共通domainやfree mailを自動で同一組織と推測しない。そうした候補は人手確認用の
    別経路が用意されるまでは採用しないことで、第三者メールの混入をfail-closedにする。
    """
    if "@" not in email:
        return False
    email_domain = _normalized_site_host(email.rsplit("@", 1)[1])
    site = _normalized_site_host(origin_host)
    return bool(email_domain and site) and (email_domain == site or email_domain.endswith("." + site))


def _is_content_page(url: str, title: str) -> bool:
    try:
        path = urlsplit(url).path
    except ValueError:
        return True
    return bool(_CONTENT_PATH_RE.search(path) or _CONTENT_TITLE_RE.search(title or ""))


def _mailto_display_conflicts(email: str, context: str) -> bool:
    """mailtoの表示文字列が別アドレスならhrefを信用しない。"""
    displayed = re.findall(r"[A-Za-z0-9][A-Za-z0-9._%+\-]*@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", context or "")
    return bool(displayed and all(item.lower() != email.lower() for item in displayed))


def _is_acceptable_email(extracted, page, origin_host: str) -> bool:
    if _is_content_page(page.url, page.title):
        return False
    if not _email_domain_matches_site(extracted.email, origin_host):
        return False
    if extracted.extraction_method == "MAILTO" and _mailto_display_conflicts(
        extracted.email, extracted.context
    ):
        return False
    return True


def _decompose_all(nodes):
    """decompose()は子孫も連鎖的に切り離すため、同じfind_all結果に入れ子のnodeが
    含まれると、先に親をdecomposeした時点で子が孤立し、子を later処理する際に
    AttributeError('NoneType' object has no attribute 'get')になる(bs4の既知の挙動)。
    判定(node.get(...)等)は全nodeに対して先に終えてから、decomposeは
    まだ木に残っている(parentがNone担っていない)nodeだけに限定して行う。"""
    for node in nodes:
        if node.parent is not None:
            node.decompose()


def _extract_non_article_emails(html: str):
    """記事本文・医師経歴ブロックを除去してから、明示されたメールだけを抽出する。"""
    soup = BeautifulSoup(html or "", "html.parser")
    _decompose_all(list(soup.find_all("article")))
    risky_container = re.compile(
        r"(?:^|[-_\s])(?:blog|news|column|archive|post|doctor[-_]?profile|doctor[-_]?career)(?:$|[-_\s])",
        re.I,
    )
    risky_by_class = [
        node for node in soup.find_all(attrs={"class": True})
        if risky_container.search(" ".join(node.get("class") or []))
    ]
    _decompose_all(risky_by_class)
    risky_by_id = [
        node for node in soup.find_all(attrs={"id": True})
        if risky_container.search(str(node.get("id") or ""))
    ]
    _decompose_all(risky_by_id)
    return extract_all_emails(str(soup))


@dataclass
class PageAttempt:
    requested_url: str
    depth: int
    status: str  # FETCHED | FETCH_FAILED
    final_url: str = ""
    redirected: bool = False
    title: str = ""
    http_error: str = ""


@dataclass
class EmailEvidence:
    email: str
    extraction_method: str
    page_url: str
    page_title: str
    evidence_text: str
    email_type: str
    other_facility_suspected: bool
    nearby_text: str = ""  # Hardening Phase 2: Role Classification用の周辺文脈(追加専用field)
    email_context: EmailContext | None = None  # Fresh Canary v2: Context Capture全field(追加専用)


@dataclass
class FormEvidence:
    page_url: str
    action: str
    form_type: str


@dataclass
class CrawlResult:
    clinic_id: str
    facility_name: str
    hp_url: str
    pages: list[PageAttempt] = field(default_factory=list)
    emails: list[EmailEvidence] = field(default_factory=list)
    forms: list[FormEvidence] = field(default_factory=list)
    research_status: str = ""  # FOUND | FORM_ONLY | NO_EMAIL_FOUND | FETCH_FAILED
    engine_version: str = ENGINE_VERSION
    # Hardening Phase 2: Official Website Trust Gate用の集約テキスト(title+可視テキスト、
    # 取得済み全ページ分をCRAWLED_TEXT_SAMPLE_MAX_CHARSまで丸めて保持。ページ全文は保存しない)。
    crawled_text_sample: str = ""


def _candidate_links(page_links: list[dict], origin_host: str, visited: set[str]) -> list[str]:
    """同一ホストの未訪問リンクを、問い合わせ/アクセス/予約/採用系を優先して返す。"""
    same_host = []
    for link in page_links:
        url = link.get("url", "")
        canon = canonical_page(url)
        if not canon or canon in visited or host(url) != origin_host:
            continue
        if _is_content_page(url, link.get("text", "")):
            continue
        same_host.append((0 if _CANDIDATE_PATH_HINTS.search(url + link.get("text", "")) else 1, canon, url))
    same_host.sort(key=lambda item: item[0])
    seen = set()
    ordered = []
    for _, canon, url in same_host:
        if canon in seen:
            continue
        seen.add(canon)
        ordered.append(url)
    return ordered


def crawl_clinic(clinic_id: str, facility_name: str, hp_url: str, *, fetcher: SafeFetcher | None = None,
                  max_pages: int = MAX_PAGES, max_depth: int = MAX_DEPTH) -> CrawlResult:
    """1医院の公式HPを安全な範囲でクロールし、メール/フォーム証跡を集める。

    fetcherを呼び出し側で共有すれば、同一ホストへのレート制限(SafeFetcher.last/delays)が
    複数医院間でも引き継がれる(同一ホストを複数医院のhp_urlが指すケースへの配慮)。
    """
    fetcher = fetcher or SafeFetcher(timeout=DEFAULT_TIMEOUT_SECONDS)
    origin_host = host(hp_url)
    result = CrawlResult(clinic_id=clinic_id, facility_name=facility_name, hp_url=hp_url)

    visited: set[str] = set()
    queue: list[tuple[str, int]] = [(hp_url, 0)]
    fetched_count = 0
    all_emails = []
    text_sample_parts: list[str] = []

    while queue and fetched_count < max_pages:
        url, depth = queue.pop(0)
        canon = canonical_page(url)
        if not canon or canon in visited:
            continue
        visited.add(canon)

        try:
            page = fetcher.fetch(url, allowed_host=origin_host)
        except WebError as exc:
            result.pages.append(PageAttempt(
                requested_url=url, depth=depth, status="FETCH_FAILED", http_error=str(exc),
            ))
            continue

        fetched_count += 1
        final_canon = canonical_page(page.url)
        result.pages.append(PageAttempt(
            requested_url=url, depth=depth, status="FETCHED", final_url=page.url,
            redirected=(final_canon != canon), title=page.title,
        ))

        page_emails = [
            email for email in _extract_non_article_emails(page.html)
            if _is_acceptable_email(email, page, origin_host)
        ]
        all_emails.extend((e, page) for e in page_emails)

        if sum(len(p) for p in text_sample_parts) < CRAWLED_TEXT_SAMPLE_MAX_CHARS:
            # Trust Gateのcopyright/footer判定にはfooter/nav込みのpage.textを使う
            # (main_textはnav/footer/header/asideを除去済みのため、ここでは使わない)。
            full_text = page.text if hasattr(page, "text") else ""
            text_sample_parts.append(f"{page.title} {full_text}"[:2000])

        for form in detect_forms(page.html):
            result.forms.append(FormEvidence(page_url=page.url, action=form.action, form_type=form.form_type))

        if depth < max_depth:
            for link_url in _candidate_links(page.links, origin_host, visited):
                if fetched_count + len(queue) >= max_pages:
                    break
                queue.append((link_url, depth + 1))

    deduped = dedupe_emails([e for e, _ in all_emails])
    page_by_email = {}
    for email, page in all_emails:
        page_by_email.setdefault(email.email.lower(), page)
    for extracted in deduped:
        page = page_by_email[extracted.email.lower()]
        evidence_text = f"{extracted.context or ''} ({extracted.extraction_method} at {page.url})".strip()
        context = extract_email_context(page.html, extracted.email, page_title=page.title)
        result.emails.append(EmailEvidence(
            email=extracted.email,
            extraction_method=extracted.extraction_method,
            page_url=page.url,
            page_title=page.title,
            evidence_text=evidence_text,
            email_type=classify_email_type(extracted.email, context_hint=f"{page.title} {extracted.context}"),
            other_facility_suspected=looks_like_other_facility(
                f"{page.title} {page.main_text[:500] if hasattr(page, 'main_text') else ''}", facility_name
            ),
            nearby_text=context.nearby_text,
            email_context=context,
        ))

    result.crawled_text_sample = " ".join(text_sample_parts)[:CRAWLED_TEXT_SAMPLE_MAX_CHARS]

    any_fetched = any(p.status == "FETCHED" for p in result.pages)
    if not any_fetched:
        result.research_status = "FETCH_FAILED"
    elif result.emails:
        result.research_status = "FOUND"
    elif result.forms:
        result.research_status = "FORM_ONLY"
    else:
        result.research_status = "NO_EMAIL_FOUND"

    return result
