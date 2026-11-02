"""公式HPメール抽出のpure function群(HTTPアクセスなし。入力はHTML/テキスト文字列のみ)。

設計方針(artifacts/clinic_email_phase0/email_crawler_design.md 準拠):
- 推測によるメール生成は一切しない。入力に明示的に存在する文字列だけを対象にする。
- mailto: > structured data(JSON-LD) > 本文正規表現 の優先順位で抽出する。
- ダミー/サンプルアドレスは除外する。
- 本モジュールは「公式ドメインかどうか」「本人確認済みか」を判定しない
  (それはCrawler側がclinics.hp_statusを見て行う。ここは文字列処理のみ)。
"""
from __future__ import annotations

from dataclasses import dataclass
import html as html_lib
import json
import re
import unicodedata

from bs4 import BeautifulSoup

_EMAIL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%+\-]*@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

_DUMMY_PATTERNS = (
    re.compile(r"@example\.(com|co\.jp|jp|net|org)$", re.I),
    re.compile(r"^(test|dummy|sample|xxx|foo|noreply|no-reply)@", re.I),
    re.compile(r"@(xxx|dummy|sample|test)\.", re.I),
    re.compile(r"\.png$|\.jpg$|\.gif$|\.svg$", re.I),
)

_RESERVATION_HINTS = ("yoyaku", "reserve", "reservation", "booking", "予約")
_RECRUIT_HINTS = ("saiyo", "recruit", "jobs", "採用", "求人")
_GENERAL_HINTS = ("info", "contact", "toiawase", "問い合わせ", "お問い合わせ")


@dataclass(frozen=True)
class ExtractedEmail:
    email: str
    extraction_method: str  # MAILTO | PAGE_TEXT | STRUCTURED_DATA
    raw_match: str
    context: str = ""


def normalize_email_text(raw: str) -> str:
    """全角混入・HTMLエンティティ・前後空白を正規化する(メール文字列自体は変造しない)。"""
    text = html_lib.unescape(raw or "")
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("＠", "@").strip()
    return text


def is_valid_email(value: str) -> bool:
    if not value or len(value) > 254:
        return False
    if not _EMAIL_RE.fullmatch(value):
        return False
    if any(pattern.search(value) for pattern in _DUMMY_PATTERNS):
        return False
    return True


def _candidates_from_text(text: str) -> list[str]:
    normalized = normalize_email_text(text)
    return [m.group(0) for m in _EMAIL_RE.finditer(normalized)]


def extract_mailto_emails(html: str) -> list[ExtractedEmail]:
    """<a href="mailto:...">からメールを抽出する。クエリパラメータ(?subject=...)は除去する。"""
    soup = BeautifulSoup(html or "", "html.parser")
    results: list[ExtractedEmail] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.lower().startswith("mailto:"):
            continue
        raw_target = href[len("mailto:"):]
        address_part = raw_target.split("?", 1)[0]
        normalized = normalize_email_text(address_part)
        if is_valid_email(normalized):
            results.append(ExtractedEmail(
                email=normalized, extraction_method="MAILTO", raw_match=href,
                context=a.get_text(strip=True),
            ))
    return results


def extract_structured_data_emails(html: str) -> list[ExtractedEmail]:
    """<script type="application/ld+json">内のemailフィールドを抽出する(再帰探索)。"""
    soup = BeautifulSoup(html or "", "html.parser")
    results: list[ExtractedEmail] = []

    def walk(node, found: list[str]):
        if isinstance(node, dict):
            for key, value in node.items():
                if key.lower() == "email" and isinstance(value, str):
                    found.append(value)
                else:
                    walk(value, found)
        elif isinstance(node, list):
            for item in node:
                walk(item, found)

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw_text = script.string or script.get_text() or ""
        try:
            data = json.loads(raw_text)
        except (json.JSONDecodeError, TypeError):
            continue
        found: list[str] = []
        walk(data, found)
        for value in found:
            normalized = normalize_email_text(value)
            if is_valid_email(normalized):
                results.append(ExtractedEmail(
                    email=normalized, extraction_method="STRUCTURED_DATA", raw_match=value,
                ))
    return results


def extract_plaintext_emails(text: str) -> list[ExtractedEmail]:
    """本文の可視テキストから正規表現でメール候補を抽出する(mailto/構造化データが無い場合のfallback)。"""
    results = []
    for candidate in _candidates_from_text(text or ""):
        if is_valid_email(candidate):
            results.append(ExtractedEmail(email=candidate, extraction_method="PAGE_TEXT", raw_match=candidate))
    return results


def extract_all_emails(html: str) -> list[ExtractedEmail]:
    """mailto: > structured data > 本文正規表現 の優先順位で抽出する(重複は除去しない。呼び出し側でdedupe_emailsを使う)。"""
    soup = BeautifulSoup(html or "", "html.parser")
    visible_text = soup.get_text(" ", strip=True)
    return [
        *extract_mailto_emails(html),
        *extract_structured_data_emails(html),
        *extract_plaintext_emails(visible_text),
    ]


def dedupe_emails(emails: list[ExtractedEmail]) -> list[ExtractedEmail]:
    """大文字小文字を無視して重複除去する。最初に見つかった抽出手法を優先して残す。"""
    seen: dict[str, ExtractedEmail] = {}
    for item in emails:
        key = item.email.lower()
        if key not in seen:
            seen[key] = item
    return list(seen.values())


def classify_email_type(email: str, context_hint: str = "") -> str:
    """ローカルパートとページ文脈の両方をヒントにGENERAL/RESERVATION/RECRUIT/OTHER/UNKNOWNへ分類する。"""
    local_part = email.split("@", 1)[0].lower()
    combined = f"{local_part} {context_hint}".lower()
    if any(hint in combined for hint in _RESERVATION_HINTS):
        return "RESERVATION"
    if any(hint in combined for hint in _RECRUIT_HINTS):
        return "RECRUIT"
    if any(hint in combined for hint in _GENERAL_HINTS):
        return "GENERAL"
    return "UNKNOWN"


_FORM_TYPE_HINTS = (
    ("RESERVATION", ("予約", "reserve", "reservation", "booking")),
    ("RECRUIT", ("採用", "求人", "recruit", "jobs")),
    ("GENERAL", ("問い合わせ", "お問い合わせ", "contact")),
)


@dataclass(frozen=True)
class DetectedForm:
    action: str
    form_type: str


def detect_forms(html: str) -> list[DetectedForm]:
    """<form>のaction属性と周辺テキストからform_typeを推定する(推測メール生成とは無関係)。"""
    soup = BeautifulSoup(html or "", "html.parser")
    results = []
    for form in soup.find_all("form"):
        action = form.get("action", "")
        surrounding = form.get_text(" ", strip=True)
        heading = form.find_previous(["h1", "h2", "h3", "h4"])
        context = f"{action} {surrounding} {heading.get_text() if heading else ''}".lower()
        form_type = "OTHER"
        for label, hints in _FORM_TYPE_HINTS:
            if any(hint in context for hint in hints):
                form_type = label
                break
        results.append(DetectedForm(action=action, form_type=form_type))
    return results


def looks_like_other_facility(evidence_text: str, expected_facility_name: str) -> bool:
    """抽出根拠の周辺文脈に、想定医院名と異なる施設名らしき記載が強く示唆されるかの簡易チェック。

    厳密な名寄せは行わない(誤検知より見逃しを許容するfail-open的な補助シグナル)。
    呼び出し側はTrueのときだけ人によるレビューに回す判断材料として使う。
    """
    if not expected_facility_name or not evidence_text:
        return False
    normalized_expected = unicodedata.normalize("NFKC", expected_facility_name)
    normalized_evidence = unicodedata.normalize("NFKC", evidence_text)
    # 医院名の主要部分(先頭4文字相当)が本文に一切現れない場合だけ「別施設の可能性」を示唆する。
    key_fragment = normalized_expected[:4]
    return bool(key_fragment) and key_fragment not in normalized_evidence
