"""READ ONLY selection of 「メール送付文章」 targets from Clinic Master.

Only ``store.query``/``store.count`` style read calls are used and the email lookup is an
injected read-only callable, so this module never writes to any DB.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .article_adapter import ArticleLink, lookup_article
from .compose import ARTICLE_LISTED, ARTICLE_NOT_LISTED, ARTICLE_UNCONFIRMED

DEFAULT_LIMIT = 50
EMAIL_FOUND = "FOUND"
EMAIL_MISSING = "MISSING"
_EMAIL = re.compile(r"^[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+$")


@dataclass(frozen=True)
class ArticleState:
    status: str  # LISTED / NOT_LISTED / UNCONFIRMED
    listed_url: str = ""
    example_url: str = ""
    reason: str = ""


@dataclass(frozen=True)
class ClinicTarget:
    clinic_id: int
    clinic_uuid: str
    clinic_name: str
    prefecture: str
    municipality: str
    departments: tuple[str, ...]
    treatments: tuple[str, ...]
    hp_rank: str
    director_name: str
    email: str
    email_status: str
    article: ArticleState = field(default_factory=lambda: ArticleState(ARTICLE_UNCONFIRMED))


def valid_email(value: str) -> bool:
    return bool(_EMAIL.fullmatch((value or "").strip()))


def classify_article(clinic_uuid: str, links: list[ArticleLink]) -> ArticleState:
    """記事あり/なし/未確認の判定。確認根拠のない断定はしない。

    - 記事あり: 既存lookup_articleがREADY（検証済み・LISTED・VERIFIED_ARTICLE・正規URL）
    - 記事なし: JOIN CSVでNOT_LISTEDかつverified=true（未掲載が確認済み）
    - それ以外（JOIN CSVなし・JOINに未登録・未検証・複数候補）: 未確認
      JOINに存在しないだけでは「記事なし」と断定しない。
    """
    looked = lookup_article(clinic_uuid, links)
    if looked.status == "READY" and looked.record is not None:
        return ArticleState(ARTICLE_LISTED, looked.record.listing_url, looked.record.example_article_url)
    key = (clinic_uuid or "").strip().lower()
    rows = [r for r in links if r.clinic_uuid.strip().lower() == key]
    if rows and all(r.listing_status == "NOT_LISTED" and r.verified for r in rows):
        return ArticleState(ARTICLE_NOT_LISTED, reason="記事JOINで未掲載確認済み")
    return ArticleState(ARTICLE_UNCONFIRMED, reason=looked.reason or "記事掲載状況が未確認")


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _tuple(value) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [x for x in re.split(r"[、,，/／\s]+", value)]
    return tuple(dict.fromkeys(_text(x) for x in (value or ()) if _text(x)))


def target_from_record(record: dict, *, email: str = "", links: list[ArticleLink] | None = None) -> ClinicTarget:
    from src.normalizer.address import extract_municipality
    from src.normalizer.clinic_name import person_from_owner
    uuid = _text(record.get("uuid"))
    email = _text(email)
    director = _text(record.get("manager_name")) or person_from_owner(record.get("owner_name", ""))
    treatments = _tuple(record.get("treatment_categories")) + _tuple(record.get("website_treatment_categories"))
    return ClinicTarget(
        clinic_id=int(record.get("id") or 0),
        clinic_uuid=uuid,
        clinic_name=_text(record.get("clinic_name")),
        prefecture=_text(record.get("prefecture")),
        municipality=extract_municipality(_text(record.get("address"))) or "",
        departments=_tuple(record.get("normalized_departments") or record.get("departments")),
        treatments=tuple(dict.fromkeys(treatments)),
        hp_rank=_text(record.get("effective_hp_rank")) or "-",
        director_name=_text(director),
        email=email,
        email_status=EMAIL_FOUND if valid_email(email) else EMAIL_MISSING,
        article=classify_article(uuid, links or []),
    )


@dataclass(frozen=True)
class Selection:
    targets: list[ClinicTarget]
    scanned: int
    skipped_no_uuid: int
    skipped_duplicate: int
    truncated: bool


def select_targets(records: Iterable[dict], *, email_lookup: Callable[[list[int]], dict[int, str]],
                   links: list[ArticleLink], email_filter: str = "指定なし",
                   article_filter: tuple[str, ...] = (), limit: int = DEFAULT_LIMIT) -> Selection:
    """Dedupe by Clinic UUID, apply email/article post-filters, cap at ``limit`` (default 50)."""
    records = list(records)
    emails = email_lookup([int(r.get("id") or 0) for r in records]) if records else {}
    seen, output = set(), []
    no_uuid = duplicate = 0
    truncated = False
    for record in records:
        target = target_from_record(record, email=emails.get(int(record.get("id") or 0), ""),
                                    links=links)
        if not target.clinic_uuid:
            no_uuid += 1
            continue
        key = target.clinic_uuid.casefold()
        if key in seen:
            duplicate += 1
            continue
        if email_filter == "取得済み" and target.email_status != EMAIL_FOUND:
            continue
        if email_filter == "未取得" and target.email_status == EMAIL_FOUND:
            continue
        if article_filter and target.article.status not in article_filter:
            continue
        if len(output) >= limit:
            truncated = True
            break
        seen.add(key)
        output.append(target)
    return Selection(output, len(records), no_uuid, duplicate, truncated)
