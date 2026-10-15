"""Read-only Gmail draft inventory and conservative duplicate checks."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from email.utils import getaddresses
from typing import Any, Iterable

DRAFT_KEY_HEADER = "X-Clinic-Lead-Draft-Key"


@dataclass(frozen=True)
class DraftIdentity:
    clinic_uuid: str
    email_candidate_id: str
    recipient: str
    template_version: str
    recommended_url: str
    listed_url: str = ""


@dataclass(frozen=True)
class ExistingDraft:
    draft_id: str
    draft_key: str
    recipients: tuple[str, ...]
    subject: str


@dataclass(frozen=True)
class DuplicateResult:
    duplicate: bool
    reason: str
    draft_id: str = ""


def build_draft_key(identity: DraftIdentity) -> str:
    payload = {
        "clinic_uuid": identity.clinic_uuid.strip().casefold(),
        "email_candidate_id": identity.email_candidate_id.strip(),
        "recipient": identity.recipient.strip().casefold(),
        "template_version": identity.template_version.strip(),
        "recommended_url": identity.recommended_url.strip(),
        "listed_url": identity.listed_url.strip(),
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"clinic-lead-v1-{digest}"


def _headers(message: dict[str, Any]) -> dict[str, str]:
    values = message.get("payload", {}).get("headers", [])
    return {str(item.get("name", "")).casefold(): str(item.get("value", "")) for item in values}


def _recipients(value: str) -> tuple[str, ...]:
    return tuple(sorted({address.strip().casefold() for _name, address in getaddresses([value]) if address}))


def list_existing_drafts(service: Any) -> list[ExistingDraft]:
    """Read IDs and selected headers only; draft bodies are intentionally not fetched."""
    draft_api = service.users().drafts()
    draft_ids: list[str] = []
    page_token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"userId": "me", "maxResults": 100}
        if page_token:
            kwargs["pageToken"] = page_token
        page = draft_api.list(**kwargs).execute()
        draft_ids.extend(str(item["id"]) for item in page.get("drafts", []) if item.get("id"))
        page_token = page.get("nextPageToken")
        if not page_token:
            break

    result: list[ExistingDraft] = []
    for draft_id in draft_ids:
        item = draft_api.get(
            userId="me",
            id=draft_id,
            format="metadata",
            metadataHeaders=["To", "Subject", DRAFT_KEY_HEADER],
        ).execute()
        headers = _headers(item.get("message", {}))
        result.append(ExistingDraft(
            draft_id=draft_id,
            draft_key=headers.get(DRAFT_KEY_HEADER.casefold(), ""),
            recipients=_recipients(headers.get("to", "")),
            subject=headers.get("subject", ""),
        ))
    return result


def check_duplicate(
    identity: DraftIdentity,
    *,
    subject: str,
    existing: Iterable[ExistingDraft],
) -> DuplicateResult:
    expected_key = build_draft_key(identity)
    recipient = identity.recipient.strip().casefold()
    for draft in existing:
        if draft.draft_key and draft.draft_key == expected_key:
            return DuplicateResult(True, "DRAFT_KEY_MATCH", draft.draft_id)
    for draft in existing:
        if recipient in draft.recipients and draft.subject == subject:
            return DuplicateResult(True, "RECIPIENT_SUBJECT_MATCH", draft.draft_id)
    return DuplicateResult(False, "NO_MATCH")
