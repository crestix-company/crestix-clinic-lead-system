"""Gmail draft preflight and explicitly gated draft creation; never sends messages."""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import stat
from datetime import date, datetime, timezone
from dataclasses import dataclass
from email import policy
from email.message import EmailMessage
from email.utils import getaddresses
from pathlib import Path
from typing import Any, Iterable

from .email_template_v2 import (
    ARTICLE_ABSENT,
    ARTICLE_PRESENT,
    ARTICLE_UNVERIFIED,
    PreviewInput,
    SUBJECT,
    TEMPLATE_VERSION,
    render_preview,
)
from .gmail_oauth import (
    DEFAULT_TOKEN_FILE,
    REQUIRED_SCOPES,
    GmailConnection,
    expected_gmail_account,
)

DRAFT_KEY_HEADER = "X-Clinic-Lead-Draft-Key"
OUTREACH_ID_HEADER = "X-Clinic-Lead-Outreach-ID"
OPERATION_ID_HEADER = "X-Clinic-Lead-Operation-ID"
CLINIC_ID_HEADER = "X-Clinic-Lead-Clinic-ID"
CLINIC_UUID_HEADER = "X-Clinic-Lead-Clinic-UUID"
CAMPAIGN_ID_HEADER = "X-Clinic-Lead-Campaign-ID"
CLINIC_NAME_HEADER = "X-Clinic-Lead-Clinic-Name"
CLINIC_ADDRESS_HEADER = "X-Clinic-Lead-Clinic-Address"
GAS_CASE_ID_HEADER = "X-Clinic-Lead-GAS-Case-ID"
DEFAULT_DRAFT_LEDGER = DEFAULT_TOKEN_FILE.with_name("draft_ledger.jsonl")
CANARY_ACCOUNT = "info@crestix-inc.com"
CANARY_CONFIRMATION = "I_APPROVE_ONE_CANARY_DRAFT_TO_INFO"
BATCH_CONFIRMATION = "I_APPROVE_BATCH_DRAFT_CREATION"
MAX_BATCH_SIZE = 25
_EMAIL_RE = re.compile(r"^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$")


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
    clinic_id: str = ""
    clinic_uuid: str = ""
    outreach_id: str = ""
    campaign_id: str = ""
    message_id: str = ""
    thread_id: str = ""
    gas_case_id: str = ""


@dataclass(frozen=True)
class ExistingMessage:
    message_id: str
    thread_id: str
    recipients: tuple[str, ...]
    subject: str
    sent_at: str = ""
    clinic_id: str = ""
    clinic_uuid: str = ""
    outreach_id: str = ""
    campaign_id: str = ""
    clinic_name: str = ""
    clinic_address: str = ""
    gas_case_id: str = ""


@dataclass(frozen=True)
class DuplicateResult:
    duplicate: bool
    reason: str
    draft_id: str = ""


@dataclass(frozen=True)
class ApprovedDraftRequest:
    """A single v2 body sourced from the approved V column; no legacy body field exists."""

    clinic_uuid: str
    email_candidate_id: str
    clinic_name: str
    director_name: str
    recipient: str
    subject: str
    v2_body: str
    human_review_status: str
    template_version: str
    article_status: str
    article_evidence_url: str
    article_evidence_method: str
    article_checked_at: str
    actual_article_url: str = ""
    recommended_article_url: str = ""
    clinic_detail_url: str = ""
    clinic_detail_verified: bool = False
    article_listing_verified: bool = False
    article_absence_verified: bool = False
    director_name_verified: bool = False
    recipient_email_verified: bool = False
    schedule: tuple[str, ...] = ()
    phone_call_date: date | None = None
    email_send_request_confirmed: bool = False
    article_master_urls: frozenset[str] = frozenset()

    @property
    def identity(self) -> DraftIdentity:
        return DraftIdentity(
            clinic_uuid=self.clinic_uuid,
            email_candidate_id=self.email_candidate_id,
            recipient=self.recipient,
            template_version=self.template_version,
            recommended_url=self.recommended_article_url or self.actual_article_url,
            listed_url=self.actual_article_url,
        )


@dataclass(frozen=True)
class DraftCreationResult:
    status: str
    draft_id: str = ""
    draft_key: str = ""
    reasons: tuple[str, ...] = ()


class DraftSafetyError(ValueError):
    """Safe validation failure; messages never include message bodies or credentials."""


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


def build_shared_draft_key(clinic_id: int, campaign_id: str) -> str:
    """Stable cross-client key shared by GAS and Clinic Lead for one clinic/campaign."""
    material = f"clinic-outreach-v1|{int(clinic_id)}|{campaign_id.strip()}".encode()
    return hashlib.sha256(material).hexdigest()


def _headers(message: dict[str, Any]) -> dict[str, str]:
    values = message.get("payload", {}).get("headers", [])
    # RFC 5322 permits folded header lines. Gmail metadata adapters (and test
    # doubles) may expose the continuation whitespace literally, while the
    # value written by _raw_message is a single token. Unfold before comparing
    # idempotency keys; trim only transport whitespace, never alter the token.
    result: dict[str, str] = {}
    for item in values:
        name = str(item.get("name", "")).casefold()
        value = re.sub(r"\r?\n[ \t]+", " ", str(item.get("value", ""))).strip(" \t\r\n")
        result[name] = value
    return result


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
            metadataHeaders=["To", "Subject", "Message-ID", "Date", DRAFT_KEY_HEADER,
                             OUTREACH_ID_HEADER, OPERATION_ID_HEADER, CLINIC_ID_HEADER,
                             CLINIC_UUID_HEADER, CAMPAIGN_ID_HEADER, CLINIC_NAME_HEADER,
                             CLINIC_ADDRESS_HEADER, GAS_CASE_ID_HEADER],
        ).execute()
        headers = _headers(item.get("message", {}))
        result.append(ExistingDraft(
            draft_id=draft_id,
            draft_key=headers.get(DRAFT_KEY_HEADER.casefold(), ""),
            recipients=_recipients(headers.get("to", "")),
            subject=headers.get("subject", ""),
            clinic_id=headers.get(CLINIC_ID_HEADER.casefold(), ""),
            clinic_uuid=headers.get(CLINIC_UUID_HEADER.casefold(), ""),
            outreach_id=headers.get(OUTREACH_ID_HEADER.casefold(), ""),
            campaign_id=headers.get(CAMPAIGN_ID_HEADER.casefold(), ""),
            message_id=str(item.get("message", {}).get("id") or ""),
            thread_id=str(item.get("message", {}).get("threadId") or ""),
            gas_case_id=headers.get(GAS_CASE_ID_HEADER.casefold(), ""),
        ))
    return result


def list_existing_messages(service: Any, *, query: str) -> list[ExistingMessage]:
    """List message metadata only. Never fetch a sent message body."""
    api = service.users().messages()
    ids: list[tuple[str, str]] = []
    page_token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"userId": "me", "q": query, "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token
        page = api.list(**kwargs).execute()
        ids.extend((str(item["id"]), str(item.get("threadId") or ""))
                   for item in page.get("messages", []) if item.get("id"))
        page_token = page.get("nextPageToken")
        if not page_token:
            break
    result = []
    metadata_headers = ["To", "Subject", "Message-ID", "Date", DRAFT_KEY_HEADER,
                        OUTREACH_ID_HEADER, OPERATION_ID_HEADER, CLINIC_ID_HEADER,
                        CLINIC_UUID_HEADER, CAMPAIGN_ID_HEADER, CLINIC_NAME_HEADER,
                        CLINIC_ADDRESS_HEADER, GAS_CASE_ID_HEADER]
    for message_id, listed_thread_id in ids:
        message = api.get(userId="me", id=message_id, format="metadata",
                          metadataHeaders=metadata_headers).execute()
        headers = _headers(message)
        result.append(ExistingMessage(
            message_id=message_id,
            thread_id=str(message.get("threadId") or listed_thread_id),
            recipients=_recipients(headers.get("to", "")),
            subject=headers.get("subject", ""),
            sent_at=headers.get("date", ""),
            clinic_id=headers.get(CLINIC_ID_HEADER.casefold(), ""),
            clinic_uuid=headers.get(CLINIC_UUID_HEADER.casefold(), ""),
            outreach_id=headers.get(OUTREACH_ID_HEADER.casefold(), ""),
            campaign_id=headers.get(CAMPAIGN_ID_HEADER.casefold(), ""),
            clinic_name=headers.get(CLINIC_NAME_HEADER.casefold(), ""),
            clinic_address=headers.get(CLINIC_ADDRESS_HEADER.casefold(), ""),
            gas_case_id=headers.get(GAS_CASE_ID_HEADER.casefold(), ""),
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


def _valid_email(value: str) -> bool:
    value = value.strip()
    addresses = getaddresses([value])
    _name, parsed = addresses[0] if len(addresses) == 1 else ("", "")
    return bool(value and parsed == value and _EMAIL_RE.fullmatch(value))


def _build_v2_preview(
    request: ApprovedDraftRequest,
    *,
    gmail_account_verified: bool,
    duplicate_check_complete: bool,
    as_of: date,
) -> tuple[Any | None, list[str]]:
    reasons: list[str] = []
    if request.human_review_status.strip() != "承認":
        reasons.append("人間確認ステータスが承認ではない")
    if request.template_version != TEMPLATE_VERSION:
        reasons.append("正式v2テンプレートバージョンが一致しない")
    if not request.clinic_uuid.strip() or not request.email_candidate_id.strip():
        reasons.append("Clinic UUIDまたはEmail Candidate IDがない")
    if not _valid_email(request.recipient):
        reasons.append("宛先メールアドレス形式が不正")
    if not request.recipient_email_verified:
        reasons.append("承認済みPrimary Emailではない")
    if request.subject != SUBJECT:
        reasons.append("件名が正式テンプレートv2と一致しない")
    if not request.v2_body.strip():
        reasons.append("承認済みv2本文が空")
    if not request.article_evidence_method.strip() or not request.article_checked_at.strip():
        reasons.append("記事確認方法または確認日時がない")
    else:
        try:
            datetime.fromisoformat(request.article_checked_at.replace("Z", "+00:00"))
        except ValueError:
            reasons.append("記事確認日時がISO形式ではない")
    if request.article_status == ARTICLE_PRESENT:
        if not request.article_listing_verified or not request.clinic_detail_verified:
            reasons.append("実掲載記事と医院の一致証拠が確認されていない")
        if request.article_evidence_url.strip() != request.actual_article_url.strip():
            reasons.append("実掲載記事URLと確認証拠URLが一致しない")
    elif request.article_status == ARTICLE_ABSENT:
        if not request.article_absence_verified or not request.clinic_detail_verified:
            reasons.append("未掲載と医院詳細ページの証拠が確認されていない")
        if request.article_evidence_url.strip() != request.clinic_detail_url.strip():
            reasons.append("医院詳細ページURLと確認証拠URLが一致しない")
        if request.article_listing_verified:
            reasons.append("ARTICLE_ABSENTに掲載済み証拠が混在")
    else:
        reasons.append("掲載状態が未確認")

    try:
        preview = render_preview(PreviewInput(
            clinic_name=request.clinic_name,
            director_name=request.director_name,
            director_name_verified=request.director_name_verified,
            article_status=request.article_status,
            actual_article_url=request.actual_article_url,
            recommended_article_url=request.recommended_article_url,
            clinic_detail_url=request.clinic_detail_url,
            clinic_detail_verified=request.clinic_detail_verified,
            article_listing_verified=request.article_listing_verified,
            article_absence_verified=request.article_absence_verified,
            schedule=request.schedule,
            sender_name="高原",
            phone_call_date=request.phone_call_date,
            email_send_request_confirmed=request.email_send_request_confirmed,
            recipient_email_verified=request.recipient_email_verified,
            gmail_account_verified=gmail_account_verified,
            human_approved=request.human_review_status.strip() == "承認",
            duplicate_check_complete=duplicate_check_complete,
            article_master_urls=request.article_master_urls,
            as_of=as_of,
        ))
    except (TypeError, ValueError):
        return None, reasons + ["正式テンプレートv2の差し込み検証に失敗"]
    if not preview.preview_generated:
        reasons.extend(preview.blockers)
    if preview.subject != request.subject:
        reasons.append("件名が正式テンプレートv2と一致しない")
    if preview.body != request.v2_body:
        reasons.append("V列の承認済みv2本文が正式テンプレートの再生成結果と一致しない")
    reasons.extend(preview.blockers)
    return preview, list(dict.fromkeys(reasons))


def _ledger_read_fd(fd: int) -> list[dict[str, Any]]:
    os.lseek(fd, 0, os.SEEK_SET)
    raw = b""
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        raw += chunk
    records: list[dict[str, Any]] = []
    for line in raw.splitlines():
        try:
            item = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DraftSafetyError("Draft audit ledger is malformed; refusing to proceed") from exc
        if not isinstance(item, dict) or not item.get("draft_key") or not item.get("status"):
            raise DraftSafetyError("Draft audit ledger record is invalid; refusing to proceed")
        records.append(item)
    return records


def _ledger_append(
    path: Path,
    *,
    draft_key: str,
    status: str,
    clinic_uuid: str,
    email_candidate_id: str,
    draft_id: str = "",
    reserve: bool = False,
) -> bool:
    """Append a private, locked JSONL audit event. A reservation is unique per Draft Key."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    if path.is_symlink() or (path.exists() and stat.S_IMODE(path.stat().st_mode) & 0o077):
        raise DraftSafetyError("Draft audit ledger permissions or file type are unsafe")
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        records = _ledger_read_fd(fd)
        if reserve and any(item.get("draft_key") == draft_key for item in records):
            return False
        record = {
            "draft_key": draft_key,
            "status": status,
            "clinic_uuid": clinic_uuid,
            "email_candidate_id": email_candidate_id,
            "draft_id": draft_id,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        encoded = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        os.lseek(fd, 0, os.SEEK_END)
        remaining = memoryview(encoded)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise DraftSafetyError("Draft audit ledger write did not complete")
            remaining = remaining[written:]
        os.fsync(fd)
        return True
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _ledger_contains(path: Path, draft_key: str) -> bool:
    if not path.exists():
        return False
    if path.is_symlink() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise DraftSafetyError("Draft audit ledger permissions or file type are unsafe")
    fd = os.open(path, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        return any(item.get("draft_key") == draft_key for item in _ledger_read_fd(fd))
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _mime_raw(request: ApprovedDraftRequest, draft_key: str) -> str:
    message = EmailMessage(policy=policy.SMTP)
    message["To"] = request.recipient
    message["Subject"] = request.subject
    message[DRAFT_KEY_HEADER] = draft_key
    message.set_content(request.v2_body, subtype="plain", charset="utf-8")
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii").rstrip("=")


def create_approved_draft(
    request: ApprovedDraftRequest,
    *,
    mode: str,
    connection: GmailConnection | None = None,
    execute: bool = False,
    confirmation: str = "",
    confirmed_batch_count: int | None = None,
    batch_size: int | None = None,
    ledger_path: Path | None = None,
    as_of: date | None = None,
) -> DraftCreationResult:
    """Dry-run by default; creates exactly one Gmail draft only after all independent gates pass."""
    if mode not in {"canary", "batch"}:
        raise DraftSafetyError("Mode must be canary or batch")
    recipient = request.recipient.strip().casefold()
    if mode == "canary" and recipient != CANARY_ACCOUNT:
        raise DraftSafetyError("Canary recipient must be info@crestix-inc.com")
    if mode == "batch" and recipient == CANARY_ACCOUNT:
        raise DraftSafetyError("The internal Canary recipient is not accepted in batch mode")
    if mode == "canary" and batch_size not in (None, 1):
        raise DraftSafetyError("Canary mode accepts exactly one candidate")
    if mode == "batch":
        if batch_size is None or not 1 <= batch_size <= MAX_BATCH_SIZE:
            raise DraftSafetyError(f"Batch size must be between 1 and {MAX_BATCH_SIZE}")
        if execute and confirmed_batch_count != batch_size:
            raise DraftSafetyError("Batch confirmation count must exactly match the candidate count")

    key = build_draft_key(request.identity)
    today = as_of or date.today()
    if not execute:
        _preview, reasons = _build_v2_preview(
            request, gmail_account_verified=False, duplicate_check_complete=False, as_of=today,
        )
        runtime_gates = {"指定Gmailアカウント未確認", "Gmail下書き重複検査未完了"}
        content_reasons = [reason for reason in reasons if reason not in runtime_gates]
        content_ready = _preview is not None and not content_reasons
        reasons.extend(("DRY_RUN: Gmail書き込みなし", "OAuth接続・既存下書き照合は未実施"))
        return DraftCreationResult(
            "DRY_RUN_CONTENT_READY" if content_ready else "DRY_RUN_NEEDS_REVIEW",
            draft_key=key,
            reasons=tuple(dict.fromkeys(reasons)),
        )

    expected = expected_gmail_account().strip().casefold()
    if connection is None or connection.email_address.strip().casefold() != expected:
        raise DraftSafetyError("OAuth接続アカウントが許可アカウントと一致しない")
    if tuple(connection.scopes) != REQUIRED_SCOPES:
        raise DraftSafetyError("OAuth scopeが必要最小限のgmail.composeと一致しない")
    required_confirmation = CANARY_CONFIRMATION if mode == "canary" else BATCH_CONFIRMATION
    if confirmation != required_confirmation:
        raise DraftSafetyError("明示的な実行確認が一致しない")

    preview, reasons = _build_v2_preview(
        request, gmail_account_verified=True, duplicate_check_complete=False, as_of=today,
    )
    content_reasons = [reason for reason in reasons if reason != "Gmail下書き重複検査未完了"]
    if preview is None or content_reasons:
        return DraftCreationResult("NEEDS_REVIEW", draft_key=key, reasons=tuple(content_reasons))

    ledger = ledger_path or Path.cwd() / DEFAULT_DRAFT_LEDGER
    try:
        if _ledger_contains(ledger, key):
            return DraftCreationResult("SKIPPED_DUPLICATE", draft_key=key,
                                       reasons=("LOCAL_LEDGER_KEY_EXISTS",))
        existing = list_existing_drafts(connection.service)
    except Exception:
        return DraftCreationResult("FAILED_PREFLIGHT", draft_key=key,
                                   reasons=("EXISTING_DRAFT_CHECK_FAILED",))
    duplicate = check_duplicate(request.identity, subject=request.subject, existing=existing)
    if duplicate.duplicate:
        return DraftCreationResult("SKIPPED_DUPLICATE", draft_id=duplicate.draft_id,
                                   draft_key=key, reasons=(duplicate.reason,))
    final_preview, final_reasons = _build_v2_preview(
        request, gmail_account_verified=True, duplicate_check_complete=True, as_of=today,
    )
    if final_preview is None or not final_preview.ready_for_draft or final_reasons:
        return DraftCreationResult("NEEDS_REVIEW", draft_key=key, reasons=tuple(final_reasons))

    try:
        reserved = _ledger_append(
            ledger, draft_key=key, status="PENDING", clinic_uuid=request.clinic_uuid,
            email_candidate_id=request.email_candidate_id, reserve=True,
        )
    except Exception:
        return DraftCreationResult("FAILED_AUDIT_LEDGER", draft_key=key,
                                   reasons=("AUDIT_LEDGER_RESERVATION_FAILED",))
    if not reserved:
        return DraftCreationResult("SKIPPED_DUPLICATE", draft_key=key,
                                   reasons=("LOCAL_LEDGER_KEY_EXISTS",))

    create_error = False
    draft_id = ""
    try:
        response = connection.service.users().drafts().create(
            userId="me",
            body={"message": {"raw": _mime_raw(request, key)}},
        ).execute()
        draft_id = str(response.get("id", "")) if isinstance(response, dict) else ""
        if not draft_id:
            create_error = True
    except Exception:
        create_error = True

    if create_error:
        try:
            after = list_existing_drafts(connection.service)
            # Recovery must prove this exact create by its idempotency header. A
            # concurrent/manual draft with the same recipient and subject is not
            # sufficient evidence that our timed-out create succeeded.
            recovered_draft = next((item for item in after if item.draft_key == key), None)
            recovered = (DuplicateResult(True, "DRAFT_KEY_MATCH", recovered_draft.draft_id)
                         if recovered_draft else DuplicateResult(False, "NO_EXACT_DRAFT_KEY_MATCH"))
        except Exception:
            recovered = DuplicateResult(False, "REQUERY_FAILED")
        if recovered.duplicate:
            try:
                _ledger_append(
                    ledger, draft_key=key, status="CREATED_RECOVERED", clinic_uuid=request.clinic_uuid,
                    email_candidate_id=request.email_candidate_id, draft_id=recovered.draft_id,
                )
            except Exception:
                return DraftCreationResult("CREATED_LEDGER_UPDATE_FAILED", draft_id=recovered.draft_id,
                                           draft_key=key, reasons=("LEDGER_UPDATE_FAILED",))
            return DraftCreationResult("CREATED_RECOVERED", draft_id=recovered.draft_id, draft_key=key,
                                       reasons=("CREATE_RESPONSE_UNCERTAIN_REQUERY_MATCHED",))
        try:
            _ledger_append(
                ledger, draft_key=key, status="UNKNOWN", clinic_uuid=request.clinic_uuid,
                email_candidate_id=request.email_candidate_id,
            )
        except Exception:
            pass
        return DraftCreationResult("OUTCOME_UNKNOWN", draft_key=key,
                                   reasons=("NO_AUTOMATIC_RETRY_AFTER_UNCERTAIN_CREATE",))

    try:
        _ledger_append(
            ledger, draft_key=key, status="CREATED", clinic_uuid=request.clinic_uuid,
            email_candidate_id=request.email_candidate_id, draft_id=draft_id,
        )
    except Exception:
        return DraftCreationResult("CREATED_LEDGER_UPDATE_FAILED", draft_id=draft_id,
                                   draft_key=key, reasons=("LEDGER_UPDATE_FAILED",))
    return DraftCreationResult("DRAFT_CREATED", draft_id=draft_id, draft_key=key)
