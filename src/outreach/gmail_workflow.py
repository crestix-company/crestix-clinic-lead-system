"""UI draft workflow: Gmail read reconciliation, one-time reservation, drafts.create only."""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from email import policy
from email.message import EmailMessage
import hashlib
import os
import uuid
from zoneinfo import ZoneInfo
from email.utils import parsedate_to_datetime

from src.outreach.generation import READY, OutreachResult
from src.outreach.gmail_drafts import (
    CAMPAIGN_ID_HEADER,
    CLINIC_ADDRESS_HEADER,
    CLINIC_ID_HEADER,
    CLINIC_NAME_HEADER,
    CLINIC_UUID_HEADER,
    DRAFT_KEY_HEADER,
    OPERATION_ID_HEADER,
    OUTREACH_ID_HEADER,
    ExistingDraft,
    ExistingMessage,
    list_existing_drafts,
    list_existing_messages,
)
from src.outreach.gmail_oauth import GmailConnection, REQUIRED_SCOPES
from src.repository.outreach_delivery_repository import (
    BLOCKING_STATUSES,
    DraftReservation,
    OutreachDeliveryRepository,
)
from src.outreach.targets import valid_email

JST = ZoneInfo("Asia/Tokyo")
SCHEDULED_QUERY = "in:scheduled"
TRASH_QUERY = "in:trash"
TARGET_GMAIL_ACCOUNT = "info@crestix-inc.com"


@dataclass(frozen=True)
class GmailState:
    drafts: tuple[ExistingDraft, ...]
    sent_today: tuple[ExistingMessage, ...]
    scheduled: tuple[ExistingMessage, ...]
    # Gmail auto-empties Trash after ~30 days, so this query is naturally bounded;
    # no additional date filter is applied. Distinguishing "trashed" from "no history"
    # matters for human review even though it never silently unblocks a new draft.
    trashed: tuple[ExistingMessage, ...] = ()
    sent_history: tuple[ExistingMessage, ...] = ()
    replies: tuple[ExistingMessage, ...] = ()


@dataclass(frozen=True)
class DraftWorkflowResult:
    status: str
    outreach_id: str = ""
    gmail_draft_id: str = ""
    gmail_message_id: str = ""
    gmail_thread_id: str = ""
    reason: str = ""


class DraftWorkflowError(RuntimeError):
    """Safe fail-closed Gmail draft workflow error."""


def gmail_drafts_enabled(environ=None) -> bool:
    env = os.environ if environ is None else environ
    return str(env.get("OUTREACH_GMAIL_DRAFTS_ENABLED", "false")).strip().casefold() == "true"


def _unix_seconds(value: datetime) -> int:
    return int(value.astimezone(timezone.utc).timestamp())


def read_gmail_state(service, *, as_of: date | None = None, target_email: str = "") -> GmailState:
    day = as_of or datetime.now(JST).date()
    start = datetime.combine(day, time.min, tzinfo=JST)
    end = start + timedelta(days=1)
    sent_query = f"in:sent after:{_unix_seconds(start)} before:{_unix_seconds(end)}"
    # If any query fails, history is unverified and no draft may be created.
    drafts = list_existing_drafts(service)
    sent = list_existing_messages(service, query=sent_query)
    scheduled = list_existing_messages(service, query=SCHEDULED_QUERY)
    trashed = list_existing_messages(service, query=TRASH_QUERY)
    sent_history = list_existing_messages(service, query=f"in:sent to:{target_email}") if target_email else []
    replies = list_existing_messages(service, query=f"in:anywhere from:{target_email}") if target_email else []
    return GmailState(tuple(drafts), tuple(sent), tuple(scheduled), tuple(trashed),
                      tuple(sent_history), tuple(replies))


def _identity_match(*, clinic_id: int, clinic_uuid: str, outreach_id: str,
                    email: str, clinic_name: str, clinic_address: str,
                    headers) -> bool:
    # Exact persistent IDs precede the recipient and name/address fallback.
    if headers.clinic_id and headers.clinic_id.isdigit() and int(headers.clinic_id) != clinic_id:
        return False
    if headers.clinic_uuid and clinic_uuid and headers.clinic_uuid.casefold() != clinic_uuid.casefold():
        return False
    if headers.clinic_id and headers.clinic_id.isdigit() and int(headers.clinic_id) == clinic_id:
        return True
    if headers.clinic_uuid and clinic_uuid and headers.clinic_uuid.casefold() == clinic_uuid.casefold():
        return True
    if headers.outreach_id and outreach_id and headers.outreach_id == outreach_id:
        return True
    if email.casefold() in headers.recipients:
        return True
    return bool(headers.clinic_name and headers.clinic_address
                and headers.clinic_name.strip().casefold() == clinic_name.strip().casefold()
                and headers.clinic_address.strip().casefold() == clinic_address.strip().casefold())


def _matching_rows(rows, *, clinic_id, clinic_uuid, outreach_id, email, clinic_name, clinic_address):
    return [row for row in rows if _identity_match(
        clinic_id=clinic_id, clinic_uuid=clinic_uuid, outreach_id=outreach_id,
        email=email, clinic_name=clinic_name, clinic_address=clinic_address, headers=row,
    )]


def classify_existing_state(result: OutreachResult, *, state: GmailState,
                             outreach_id: str = "") -> str:
    return classify_target_state(result.target, state=state, outreach_id=outreach_id)


def classify_target_state(target, *, state: GmailState, outreach_id: str = "") -> str:
    args = dict(clinic_id=target.clinic_id, clinic_uuid=target.clinic_uuid,
                outreach_id=outreach_id, email=target.email, clinic_name=target.clinic_name,
                clinic_address=target.clinic_address)
    observed_rows = (*state.drafts, *state.sent_today, *state.sent_history,
                     *state.scheduled, *state.trashed, *state.replies)
    for row in observed_rows:
        explicit_id = str(getattr(row, "clinic_id", "") or "").strip()
        explicit_uuid = str(getattr(row, "clinic_uuid", "") or "").strip().casefold()
        recipients = getattr(row, "recipients", ())
        if (explicit_id.isdigit() and int(explicit_id) != target.clinic_id
                and target.email.casefold() in {str(x).casefold() for x in recipients}):
            return "IDENTITY_MISMATCH"
        if (explicit_uuid and target.clinic_uuid and explicit_uuid != target.clinic_uuid.casefold()
                and target.email.casefold() in {str(x).casefold() for x in recipients}):
            return "IDENTITY_MISMATCH"
    sent_rows = _matching_rows(state.sent_history or state.sent_today, **args)
    reply_rows = _matching_rows(state.replies, **args)
    sent_threads = {row.thread_id for row in sent_rows if row.thread_id}
    if reply_rows or any(row.thread_id and row.thread_id in sent_threads for row in state.replies):
        return "REPLIED_EXISTS"
    if sent_rows:
        return "SENT_TODAY"
    if len(_matching_rows(state.scheduled, **args)) > 0:
        return "SCHEDULED_EXISTS"
    if len(_matching_rows(state.drafts, **args)) > 0:
        return "DRAFT_EXISTS"
    # Trashed is distinguished from "no history" (not silently treated as CLEAR) so a
    # manually deleted draft/message still routes to human review instead of a fresh send.
    if len(_matching_rows(state.trashed, **args)) > 0:
        return "TRASHED_EXISTS"
    return "CLEAR"


def _draft_key(clinic_id: int, campaign_id: str) -> str:
    material = f"clinic-outreach-v1|{int(clinic_id)}|{campaign_id.strip()}".encode()
    return hashlib.sha256(material).hexdigest()


def _raw_message(result: OutreachResult, *, campaign_id: str,
                 reservation: DraftReservation, sender_account: str) -> str:
    target = result.target
    message = EmailMessage(policy=policy.SMTP)
    message["To"] = target.email
    message["From"] = sender_account
    message["Subject"] = result.subject
    message[DRAFT_KEY_HEADER] = _draft_key(target.clinic_id, campaign_id)
    message[OUTREACH_ID_HEADER] = reservation.outreach_id
    message[OPERATION_ID_HEADER] = reservation.operation_id
    message[CLINIC_ID_HEADER] = str(target.clinic_id)
    message[CLINIC_UUID_HEADER] = target.clinic_uuid
    message[CAMPAIGN_ID_HEADER] = campaign_id
    message[CLINIC_NAME_HEADER] = target.clinic_name
    message[CLINIC_ADDRESS_HEADER] = target.clinic_address
    message.set_content(result.body, subtype="plain", charset="utf-8")
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii").rstrip("=")


def _create(service, *, raw: str):
    return service.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()


def _recover_exact_draft(service, *, draft_key: str):
    matches = [item for item in list_existing_drafts(service) if item.draft_key == draft_key]
    return matches[0] if len(matches) == 1 else None


def _matching_mail(rows, *, target, outreach_id: str):
    return _matching_rows(rows, clinic_id=target.clinic_id, clinic_uuid=target.clinic_uuid,
                          outreach_id=outreach_id, email=target.email,
                          clinic_name=target.clinic_name, clinic_address=target.clinic_address)


def _reconcile_existing_ledger(result: OutreachResult, *, campaign_id: str,
                               state: GmailState, repository: OutreachDeliveryRepository):
    target = result.target
    rows = [row for row in repository.campaign_rows(campaign_id)
            if int(row["clinic_id"]) == target.clinic_id]
    if not rows:
        return None
    if len(rows) != 1:
        return DraftWorkflowResult("REVIEW", reason="Multiple outreach ledger rows require human review")
    row = rows[0]
    outreach_id = str(row["outreach_id"])
    status = str(row["status"])
    draft_key = _draft_key(target.clinic_id, campaign_id)
    matching_drafts = [item for item in state.drafts if item.draft_key == draft_key]
    if len(matching_drafts) == 1 and status in {"REVIEW", "UNKNOWN", "DRAFT"}:
        draft = matching_drafts[0]
        try:
            repository.sync_status(outreach_id=outreach_id, status="DRAFT",
                                   gmail_draft_id=draft.draft_id,
                                   gmail_message_id=draft.message_id,
                                   gmail_thread_id=draft.thread_id)
        except Exception:
            return DraftWorkflowResult("UNKNOWN", outreach_id=outreach_id,
                                       gmail_draft_id=draft.draft_id,
                                       reason="Existing draft found; ledger reconciliation failed")
        return DraftWorkflowResult("DRAFT_RECOVERED", outreach_id=outreach_id,
                                   gmail_draft_id=draft.draft_id,
                                   reason="Existing draft reconciled by exact idempotency key")
    if len(matching_drafts) > 1:
        return DraftWorkflowResult("REVIEW", outreach_id=outreach_id,
                                   reason="Multiple drafts share the exact idempotency key")
    outbound = _matching_mail(state.sent_history or state.sent_today, target=target, outreach_id=outreach_id)
    sent_threads = {row.thread_id for row in outbound if row.thread_id}
    replies = _matching_mail(state.replies, target=target, outreach_id=outreach_id)
    if replies or any(row.thread_id and row.thread_id in sent_threads for row in state.replies):
        return DraftWorkflowResult("REPLIED_EXISTS", outreach_id=outreach_id,
                                   gmail_message_id=replies[0].message_id,
                                   gmail_thread_id=replies[0].thread_id,
                                   reason="Reply history exists; automatic recreation is blocked")
    sent = outbound
    if len(sent) == 1 and status in {"REVIEW", "UNKNOWN", "DRAFT"}:
        message = sent[0]
        sent_at = None
        if message.sent_at:
            try:
                sent_at = parsedate_to_datetime(message.sent_at)
            except (TypeError, ValueError, OverflowError):
                pass
        try:
            repository.sync_status(outreach_id=outreach_id, status="SENT",
                                   gmail_draft_id=str(row.get("gmail_draft_id") or ""),
                                   gmail_message_id=message.message_id,
                                   gmail_thread_id=message.thread_id, sent_at=sent_at)
        except Exception:
            return DraftWorkflowResult("UNKNOWN", outreach_id=outreach_id,
                                       gmail_message_id=message.message_id,
                                       reason="Sent message found; ledger reconciliation failed")
        return DraftWorkflowResult("SENT_TODAY", outreach_id=outreach_id,
                                   gmail_message_id=message.message_id,
                                   gmail_thread_id=message.thread_id,
                                   reason="Existing ledger reconciled to today's sent message")
    scheduled = _matching_mail(state.scheduled, target=target, outreach_id=outreach_id)
    if len(scheduled) == 1 and status in {"REVIEW", "UNKNOWN", "DRAFT"}:
        message = scheduled[0]
        try:
            repository.sync_status(outreach_id=outreach_id, status="SCHEDULED",
                                   gmail_draft_id=str(row.get("gmail_draft_id") or ""),
                                   gmail_message_id=message.message_id,
                                   gmail_thread_id=message.thread_id)
        except Exception:
            return DraftWorkflowResult("UNKNOWN", outreach_id=outreach_id,
                                       gmail_message_id=message.message_id,
                                       reason="Scheduled message found; ledger reconciliation failed")
        return DraftWorkflowResult("SCHEDULED_EXISTS", outreach_id=outreach_id,
                                   gmail_message_id=message.message_id,
                                   gmail_thread_id=message.thread_id,
                                   reason="Existing ledger reconciled to scheduled message")
    # A missing Gmail draft may have been sent or manually removed. Never convert that
    # condition into FAILED or create a replacement automatically.
    if status in {"DRAFT", "UNKNOWN", "REVIEW"}:
        return DraftWorkflowResult("UNKNOWN", outreach_id=outreach_id,
                                   gmail_draft_id=str(row.get("gmail_draft_id") or ""),
                                   reason="Existing ledger row has no exact Gmail state; manual review required")
    return DraftWorkflowResult("EXISTING_LEDGER", outreach_id=outreach_id,
                               gmail_draft_id=str(row.get("gmail_draft_id") or ""),
                               reason=f"Existing ledger state blocks a second draft: {status}")


def _reserve(repository: OutreachDeliveryRepository, *, target, result: OutreachResult,
            campaign_id: str, generation_batch_id: str, staff_name: str,
            sender_account: str) -> DraftReservation:
    return repository.reserve_draft(
        clinic_id=target.clinic_id,
        clinic_uuid=target.clinic_uuid,
        clinic_name=target.clinic_name,
        clinic_address=target.clinic_address,
        recipient_email=target.email,
        sender_account=sender_account,
        staff_name=staff_name,
        campaign_id=campaign_id,
        generation_batch_id=str(uuid.UUID(generation_batch_id)),
        operation_id=str(uuid.uuid4()),
        listing_status=target.article.status,
        article_type=target.article.article_type,
        example_article_url=target.article.example_url,
        listing_article_url=target.article.listed_url,
        clinic_profile_url=target.article.clinic_profile_url,
        subject=result.subject,
        body_sha256=hashlib.sha256(result.body.encode("utf-8")).hexdigest(),
    )


def create_gmail_draft(
    result: OutreachResult,
    *,
    campaign_id: str,
    generation_batch_id: str,
    staff_name: str,
    gmail: GmailConnection,
    repository: OutreachDeliveryRepository,
    as_of: date | None = None,
    enabled: bool | None = None,
) -> DraftWorkflowResult:
    """Create one draft after Gmail/ledger checks. No call to any send endpoint exists."""
    if not (gmail_drafts_enabled() if enabled is None else enabled):
        return DraftWorkflowResult("DISABLED", reason="OUTREACH_GMAIL_DRAFTS_ENABLED is not true")
    if not campaign_id.strip():
        return DraftWorkflowResult("REVIEW", reason="Campaign ID is required")
    target = result.target
    if result.status != READY:
        return DraftWorkflowResult("REVIEW", reason="Generated message is not READY")
    if not target.clinic_id or not target.clinic_uuid or not target.clinic_name.strip():
        return DraftWorkflowResult("REVIEW", reason="Clinic identity is incomplete")
    if not target.director_name.strip() or not target.director_name_confirmed:
        return DraftWorkflowResult("REVIEW", reason="Verified director name is required")
    if target.email_status != "FOUND" or not valid_email(target.email):
        return DraftWorkflowResult("REVIEW", reason="Verified recipient email is required")
    if target.article.status == "LISTED" and target.article.article_type != "NORMAL":
        reason = "PR/supervised/unknown listing type is excluded from draft creation"
        return DraftWorkflowResult("REVIEW", reason=reason)
    if target.article.status == "UNCONFIRMED":
        return DraftWorkflowResult("REVIEW", reason="Article verification is incomplete")
    if not gmail.email_address or gmail.email_address.casefold() != TARGET_GMAIL_ACCOUNT:
        return DraftWorkflowResult("REVIEW", reason="Gmail account verification failed")
    if tuple(gmail.scopes) != REQUIRED_SCOPES:
        return DraftWorkflowResult("REVIEW", reason="Gmail OAuth scopes do not match the required set")
    if not repository.health_check():
        return DraftWorkflowResult("REVIEW", reason="Outreach history database health check failed")

    try:
        other_campaign_rows = [
            row for row in repository.clinic_statuses([target.clinic_id]).get(target.clinic_id, ())
            if str(row.get("campaign_id") or "") != campaign_id
        ]
    except Exception:
        return DraftWorkflowResult("REVIEW", reason="Cross-campaign outreach history could not be verified")
    blocking = [row for row in other_campaign_rows if str(row.get("status") or "") in BLOCKING_STATUSES]
    if blocking:
        # This clinic already has delivery history under a different campaign_id. The
        # per-(clinic_id, campaign_id) ledger uniqueness below cannot see across
        # campaigns, so this must be checked explicitly before any reservation/Gmail call.
        return DraftWorkflowResult(
            "REVIEW",
            reason=(
                "Clinic already has "
                f"{str(blocking[0].get('status') or '')} history under campaign "
                f"{str(blocking[0].get('campaign_id') or '')}; manual review required before a new campaign"
            ),
        )

    try:
        state = read_gmail_state(gmail.service, as_of=as_of, target_email=target.email)
    except Exception:
        return DraftWorkflowResult("REVIEW", reason="Gmail history could not be verified")
    try:
        reconciled = _reconcile_existing_ledger(result, campaign_id=campaign_id,
                                                state=state, repository=repository)
    except Exception:
        return DraftWorkflowResult("REVIEW", reason="Existing outreach history could not be verified")
    if reconciled is not None:
        return reconciled
    existing = classify_existing_state(result, state=state)
    if existing == "TRASHED_EXISTS":
        # Gmail Trash auto-expires (~30 days); persist this as a durable ledger REVIEW row
        # so a later run -- possibly under a different campaign_id, after this evidence has
        # aged out of Gmail -- is still held by the cross-campaign check above, instead of
        # silently reopening once Gmail's own trace of it disappears. Never auto-recreated.
        try:
            reservation = _reserve(repository, target=target, result=result,
                                   campaign_id=campaign_id, generation_batch_id=generation_batch_id,
                                   staff_name=staff_name, sender_account=gmail.email_address)
            if reservation.created:
                repository.record_draft_result(
                    outreach_id=reservation.outreach_id, status="REVIEW",
                    last_error="Trashed Gmail message/draft matched by identity; held for manual review",
                )
        except Exception:
            pass  # best-effort persistence only; the TRASHED_EXISTS result below still blocks this call
        return DraftWorkflowResult(
            "TRASHED_EXISTS",
            reason="Existing Gmail state blocks new draft (trashed; held for review, never auto-recreated)",
        )
    if existing != "CLEAR":
        return DraftWorkflowResult(existing, reason="Existing Gmail state blocks new draft")

    try:
        reservation = _reserve(repository, target=target, result=result,
                               campaign_id=campaign_id, generation_batch_id=generation_batch_id,
                               staff_name=staff_name, sender_account=gmail.email_address)
    except Exception:
        return DraftWorkflowResult("REVIEW", reason="Outreach history reservation failed")
    if not reservation.created:
        return DraftWorkflowResult("REVIEW", outreach_id=reservation.outreach_id,
                                   gmail_draft_id=reservation.gmail_draft_id,
                                   reason=f"Existing ledger state blocks draft creation: {reservation.status}")

    key = _draft_key(target.clinic_id, campaign_id)
    try:
        response = _create(gmail.service, raw=_raw_message(
            result, campaign_id=campaign_id, reservation=reservation,
            sender_account=gmail.email_address,
        ))
        draft_id = str(response.get("id") or "") if isinstance(response, dict) else ""
        message = response.get("message") or {} if isinstance(response, dict) else {}
        message_id = str(message.get("id") or "")
        thread_id = str(message.get("threadId") or "")
        if not draft_id:
            raise TimeoutError("draft response missing ID")
    except Exception:
        # The outcome may be uncertain. Query Gmail once using the idempotency header;
        # never repeat drafts.create when there is no exact result.
        try:
            recovered = _recover_exact_draft(gmail.service, draft_key=key)
        except Exception:
            recovered = None
        if recovered is not None:
            try:
                repository.record_draft_result(
                    outreach_id=reservation.outreach_id, status="DRAFT",
                    gmail_draft_id=recovered.draft_id,
                    gmail_message_id=recovered.message_id,
                    gmail_thread_id=recovered.thread_id,
                )
            except Exception:
                return DraftWorkflowResult("UNKNOWN", outreach_id=reservation.outreach_id,
                                           gmail_draft_id=recovered.draft_id,
                                           reason="Draft found but history update failed; do not retry")
            return DraftWorkflowResult("DRAFT_RECOVERED", outreach_id=reservation.outreach_id,
                                       gmail_draft_id=recovered.draft_id,
                                       reason="Draft found by exact idempotency header after uncertain response")
        try:
            repository.record_draft_result(
                outreach_id=reservation.outreach_id, status="UNKNOWN",
                last_error="Gmail create outcome uncertain; exact draft lookup found no match",
            )
        except Exception:
            pass
        return DraftWorkflowResult("UNKNOWN", outreach_id=reservation.outreach_id,
                                   reason="Create outcome uncertain; automatic retry blocked")

    try:
        updated = repository.record_draft_result(
            outreach_id=reservation.outreach_id, status="DRAFT", gmail_draft_id=draft_id,
            gmail_message_id=message_id, gmail_thread_id=thread_id,
        )
    except Exception:
        updated = False
    if not updated:
        return DraftWorkflowResult("UNKNOWN", outreach_id=reservation.outreach_id,
                                   gmail_draft_id=draft_id, gmail_message_id=message_id,
                                   gmail_thread_id=thread_id,
                                   reason="Draft created but ledger update failed; do not retry")
    return DraftWorkflowResult("DRAFT_CREATED", outreach_id=reservation.outreach_id,
                               gmail_draft_id=draft_id, gmail_message_id=message_id,
                               gmail_thread_id=thread_id)


__all__ = ["GmailState", "DraftWorkflowResult", "DraftWorkflowError", "gmail_drafts_enabled",
           "read_gmail_state", "classify_existing_state", "classify_target_state",
           "create_gmail_draft", "SCHEDULED_QUERY"]
