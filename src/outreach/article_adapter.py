"""Read-only adapter for previously prepared Mynavi article mappings.

This module does not scrape Mynavi, infer clinic identities, or query production.
Rows must already have an exact Clinic UUID relationship recorded by a trusted
upstream article-join workflow. Ambiguity always goes to human review.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit

from .email_template import DraftValidationError


@dataclass(frozen=True)
class ArticleLink:
    clinic_uuid: str
    listing_status: str
    example_article_url: str
    listing_url: str
    listing_kind: str
    source_row: str
    article_id: str
    verified: bool
    evidence: str = ""
    checked_at: str = ""
    processing_version: str = ""
    # These are optional adapter fields until the upstream article system publishes them.
    # Missing values remain UNKNOWN/blank; they are never inferred from URL text.
    article_type: str = "UNKNOWN"
    clinic_profile_url: str = ""


@dataclass(frozen=True)
class ArticleLookup:
    status: str  # READY / NEEDS_REVIEW / NO_MAPPING
    record: ArticleLink | None
    reason: str


def _valid_mynavi_url(value: str, prefix: str) -> bool:
    if not isinstance(value, str) or not value:
        return False
    part = urlsplit(value)
    return (
        part.scheme == "https"
        and part.hostname == "clinic.mynavi.jp"
        and part.username is None
        and part.password is None
        and not part.query
        and not part.fragment
        and part.path.startswith(prefix)
    )


def lookup_article(clinic_uuid: str, records: Iterable[ArticleLink]) -> ArticleLookup:
    """Exact UUID-only lookup. Never fuzzy match, silently select, or fabricate URLs."""
    target = (clinic_uuid or "").strip().lower()
    if not target:
        return ArticleLookup("NEEDS_REVIEW", None, "Clinic UUIDなし")
    matches = [r for r in records if r.clinic_uuid.strip().lower() == target]
    if not matches:
        return ArticleLookup("NO_MAPPING", None, "記事マスターとの紐付けなし")
    # Multiple rows can be legitimate listed articles. A later review feature
    # may expose them; v1 intentionally makes no automatic arbitrary choice.
    if len(matches) != 1:
        return ArticleLookup("NEEDS_REVIEW", None, "同一Clinic UUIDの記事候補が複数存在")
    record = matches[0]
    if not record.verified or not record.source_row or not record.article_id:
        return ArticleLookup("NEEDS_REVIEW", None, "記事紐付け根拠または検証が不足")
    if not _valid_mynavi_url(record.example_article_url, "/article/"):
        return ArticleLookup("NEEDS_REVIEW", None, "記事例URLが不正")
    if record.listing_status != "LISTED" or record.listing_kind != "VERIFIED_ARTICLE":
        return ArticleLookup("NEEDS_REVIEW", None, "記事掲載未確認: 固定文面の断定を防止")
    if not _valid_mynavi_url(record.listing_url, "/article/"):
        return ArticleLookup("NEEDS_REVIEW", None, "掲載記事URLが不正")
    return ArticleLookup("READY", record, "")


def load_article_links_csv(path: str | Path) -> list[ArticleLink]:
    """Load prepared join results, never edit the article master itself.

    Input is a *joined* CSV with clinic_uuid. A raw five-column article master
    alone cannot establish which clinic is in an article and must not be passed.
    """
    required = {
        "clinic_uuid", "listing_status", "example_article_url", "listing_url",
        "listing_kind", "source_row", "article_id", "verified",
    }
    with Path(path).open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
            raise DraftValidationError("記事JOIN CSVに必要列がありません。原本記事一覧は直接使えません")
        rows = []
        for row in reader:
            flag = (row["verified"] or "").strip().lower()
            if flag not in {"true", "false"}:
                raise DraftValidationError("記事JOIN CSVのverified列が不正です")
            rows.append(ArticleLink(
                clinic_uuid=(row["clinic_uuid"] or "").strip(),
                listing_status=(row["listing_status"] or "").strip(),
                example_article_url=(row["example_article_url"] or "").strip(),
                listing_url=(row["listing_url"] or "").strip(),
                listing_kind=(row["listing_kind"] or "").strip(),
                source_row=(row["source_row"] or "").strip(),
                article_id=(row["article_id"] or "").strip(),
                verified=flag == "true",
                evidence=(row.get("evidence") or "").strip(),
                checked_at=(row.get("checked_at") or "").strip(),
                processing_version=(row.get("processing_version") or "").strip(),
                article_type=(row.get("article_type") or "UNKNOWN").strip().upper(),
                clinic_profile_url=(row.get("clinic_profile_url") or "").strip(),
            ))
    return rows
