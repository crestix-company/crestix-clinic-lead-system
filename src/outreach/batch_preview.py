"""Pure preview-only orchestration for approved article links and clinic contacts.

No Gmail, DB, network or background worker side effects.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re
from typing import Iterable

from .article_adapter import ArticleLink, lookup_article
from .email_template import DraftContent, DraftInput, DraftValidationError, render_draft


_EMAIL = re.compile(r"^[^\s@,;]+@[^\s@,;]+\.[^\s@,;]+$")


@dataclass(frozen=True)
class ClinicContact:
    clinic_uuid: str
    clinic_name: str
    primary_email: str
    recipient_role: str
    recipient_name: str
    email_verified: bool


@dataclass(frozen=True)
class EmailPreview:
    clinic_uuid: str
    status: str  # READY / NEEDS_REVIEW / NO_MAPPING
    to: str
    subject: str
    body: str
    reason: str


def preview_batch(
    contacts: Iterable[ClinicContact],
    article_links: Iterable[ArticleLink],
    *,
    is_staff_name: str,
    candidate_dates: tuple[date, ...],
    called_today_and_told_to_email: bool,
    director_attendance_requested: bool,
) -> list[EmailPreview]:
    """Deterministic preview only. Unverified email or article rows never become READY."""
    links = list(article_links)
    clinics = list(contacts)
    counts = {}
    for c in clinics:
        key = c.clinic_uuid.strip().casefold()
        counts[key] = counts.get(key, 0) + 1
    output = []
    for clinic in clinics:
        clinic_uuid = clinic.clinic_uuid.strip()
        to = clinic.primary_email.strip()
        if not clinic_uuid or counts[clinic_uuid.casefold()] != 1:
            output.append(EmailPreview(clinic_uuid, "NEEDS_REVIEW", "", "", "", "Clinic UUIDの欠損または重複"))
            continue
        if not clinic.email_verified or not _EMAIL.fullmatch(to):
            output.append(EmailPreview(clinic_uuid, "NEEDS_REVIEW", "", "", "", "宛先メール未確認"))
            continue
        article = lookup_article(clinic_uuid, links)
        if article.status != "READY" or article.record is None:
            output.append(EmailPreview(clinic_uuid, article.status, to, "", "", article.reason))
            continue
        try:
            record = article.record
            rendered: DraftContent = render_draft(DraftInput(
                clinic_name=clinic.clinic_name,
                recipient_role=clinic.recipient_role,
                recipient_name=clinic.recipient_name,
                is_staff_name=is_staff_name,
                example_article_url=record.example_article_url,
                listing_url=record.listing_url,
                listing_kind=record.listing_kind,
                listing_verified=record.verified,
                called_today_and_told_to_email=called_today_and_told_to_email,
                director_attendance_requested=director_attendance_requested,
                candidate_dates=candidate_dates,
            ))
        except DraftValidationError as error:
            output.append(EmailPreview(clinic_uuid, "NEEDS_REVIEW", to, "", "", str(error)))
            continue
        output.append(EmailPreview(clinic_uuid, "READY", to, rendered.subject, rendered.body, ""))
    return output
