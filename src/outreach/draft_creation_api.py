"""Offline core contract for the future shared GAS/Clinic Lead draft API.

This module performs no network or database I/O itself. Production adapters must
implement atomic lease methods in Postgres and inject Gmail metadata/create calls.
It never sends or schedules mail.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import uuid
from typing import Callable, Protocol, Sequence

from src.outreach.gmail_drafts import (
    CAMPAIGN_ID_HEADER, CLINIC_ADDRESS_HEADER, CLINIC_ID_HEADER, CLINIC_NAME_HEADER,
    CLINIC_UUID_HEADER, DRAFT_KEY_HEADER, GAS_CASE_ID_HEADER, OPERATION_ID_HEADER,
    OUTREACH_ID_HEADER, ExistingDraft, ExistingMessage,
    build_shared_draft_key,
)

UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)


@dataclass(frozen=True)
class DraftApiRequest:
    operation_key: str
    clinic_uuid: str
    clinic_id: int
    campaign_id: str
    recipient: str
    subject: str
    body: str
    identity_verified: bool = False
    clinic_name: str = ""
    clinic_address: str = ""
    gas_case_id: str = ""

    def validate(self) -> str:
        if not self.operation_key.strip():
            return "operation_key is required"
        if not self.identity_verified:
            return "Clinic UUID must come from a confirmed identity link"
        if not UUID_PATTERN.fullmatch(self.clinic_uuid or "") or self.clinic_id <= 0:
            return "verified Clinic UUID and clinic_id are required"
        if not self.campaign_id.strip() or not self.recipient.strip() or "@" not in self.recipient:
            return "campaign and recipient are required"
        if not self.subject.strip() or not self.body.strip():
            return "subject and body are required"
        return ""

    @property
    def draft_key(self) -> str:
        return build_shared_draft_key(self.clinic_id, self.campaign_id)

    @property
    def gmail_headers(self) -> dict[str, str]:
        """Required metadata headers for exact recovery by either caller."""
        return {
            DRAFT_KEY_HEADER: self.draft_key,
            OUTREACH_ID_HEADER: self.operation_key,
            OPERATION_ID_HEADER: self.operation_key,
            CLINIC_ID_HEADER: str(self.clinic_id),
            CLINIC_UUID_HEADER: self.clinic_uuid,
            CAMPAIGN_ID_HEADER: self.campaign_id,
            CLINIC_NAME_HEADER: self.clinic_name,
            CLINIC_ADDRESS_HEADER: self.clinic_address,
            GAS_CASE_ID_HEADER: self.gas_case_id,
        }


@dataclass(frozen=True)
class DraftLease:
    operation_key: str
    owner_token: str
    lease_expires_at: datetime
    acquired: bool
    status: str = "PENDING"
    draft_id: str = ""
    fencing_token: int = 0


@dataclass(frozen=True)
class DraftApiResult:
    status: str
    operation_key: str = ""
    clinic_uuid: str = ""
    gas_case_id: str = ""
    draft_id: str = ""
    message_id: str = ""
    thread_id: str = ""
    reason: str = ""


class DraftLeaseStore(Protocol):
    def acquire(self, *, operation_key: str, clinic_uuid: str, clinic_id: int,
                campaign_id: str, gas_case_id: str, owner_token: str,
                lease_seconds: int) -> DraftLease: ...
    def complete(self, *, operation_key: str, owner_token: str, draft_id: str,
                 message_id: str, thread_id: str, fencing_token: int) -> bool: ...
    def reconcile_existing(self, *, operation_key: str, clinic_uuid: str, clinic_id: int,
                           campaign_id: str, draft_id: str, message_id: str,
                           thread_id: str) -> bool: ...
    def observe_existing(self, *, operation_key: str, clinic_uuid: str, clinic_id: int,
                         campaign_id: str, status: str, draft_id: str,
                         message_id: str, thread_id: str) -> bool: ...
    def mark_unknown(self, *, operation_key: str, owner_token: str, reason: str) -> None: ...


@dataclass(frozen=True)
class MailboxSnapshot:
    drafts: Sequence[ExistingDraft] = ()
    scheduled: Sequence[ExistingMessage] = ()
    sent: Sequence[ExistingMessage] = ()
    trashed: Sequence[ExistingMessage] = ()
    replies: Sequence[ExistingMessage] = ()
    verified: bool = True


def _matching(items, request: DraftApiRequest):
    matched = []
    for item in items:
        item_uuid = str(getattr(item, "clinic_uuid", "") or "").strip().casefold()
        if item_uuid and item_uuid != request.clinic_uuid.casefold():
            # Same operation/recipient with another UUID is a hard conflict.
            if (getattr(item, "outreach_id", "") == request.operation_key
                    or request.recipient.casefold() in getattr(item, "recipients", ())):
                return None
            continue
        exact_key = getattr(item, "draft_key", "") == request.draft_key
        exact_operation = getattr(item, "outreach_id", "") == request.operation_key
        exact_case = bool(request.gas_case_id and getattr(item, "gas_case_id", "") == request.gas_case_id)
        exact_ids = exact_key or exact_operation or exact_case
        if exact_ids:
            matched.append(item)
        elif (item_uuid and item_uuid == request.clinic_uuid.casefold()) or (
                request.recipient.casefold() in getattr(item, "recipients", ())):
            # A same-clinic or same-recipient record without an exact operation key
            # is legacy/ambiguous history. Hold rather than creating another draft.
            return None
    return matched


def _result_from_mail_state(request: DraftApiRequest, snapshot: MailboxSnapshot) -> DraftApiResult | None:
    if not snapshot.verified:
        return DraftApiResult("HOLD", request.operation_key, request.clinic_uuid,
                              request.gas_case_id, reason="Gmail state could not be verified")
    # Draft IDs are recoverable only by exact shared idempotency header. Other states
    # require exact operation/case identity; recipient-only matches are conflicts.
    found_drafts = [d for d in snapshot.drafts if d.draft_key == request.draft_key]
    for draft in found_drafts:
        draft_uuid = str(draft.clinic_uuid or "").strip().casefold()
        draft_clinic_id = str(draft.clinic_id or "").strip()
        if ((draft_uuid and draft_uuid != request.clinic_uuid.casefold())
                or (draft_clinic_id and draft_clinic_id.isdigit()
                    and int(draft_clinic_id) != request.clinic_id)
                or (draft.campaign_id and draft.campaign_id != request.campaign_id)):
            return DraftApiResult("IDENTITY_CONFLICT", request.operation_key, request.clinic_uuid,
                                  request.gas_case_id, reason="Exact Draft key carries conflicting identity metadata")
    for draft in snapshot.drafts:
        if draft.draft_key == request.draft_key:
            continue
        draft_uuid = str(draft.clinic_uuid or "").strip().casefold()
        same_clinic = bool(draft_uuid and draft_uuid == request.clinic_uuid.casefold())
        same_recipient = request.recipient.casefold() in draft.recipients
        if same_clinic or same_recipient:
            return DraftApiResult("HOLD", request.operation_key, request.clinic_uuid,
                                  request.gas_case_id,
                                  reason="Existing non-exact Draft history requires review")
    other_matches = []
    for bucket, status in ((snapshot.scheduled, "SCHEDULED"), (snapshot.sent, "SENT"),
                           (snapshot.trashed, "TRASHED"), (snapshot.replies, "REPLIED")):
        rows = _matching(bucket, request)
        if rows is None:
            return DraftApiResult("IDENTITY_CONFLICT", request.operation_key, request.clinic_uuid,
                                  request.gas_case_id, reason="Gmail identity contradicts Clinic UUID")
        other_matches.extend((row, status) for row in rows)
    if len(found_drafts) > 1 or len(other_matches) > 1 or (found_drafts and other_matches):
        return DraftApiResult("HOLD", request.operation_key, request.clinic_uuid,
                              request.gas_case_id, reason="Conflicting or multiple exact Gmail states require review")
    if other_matches:
        row, status = other_matches[0]
        return DraftApiResult(status + "_EXISTS", request.operation_key, request.clinic_uuid,
                              request.gas_case_id, message_id=row.message_id, thread_id=row.thread_id,
                              reason="Existing Gmail state blocks Draft creation")
    if found_drafts:
        row = found_drafts[0]
        return DraftApiResult("DRAFT_RECOVERED", request.operation_key, request.clinic_uuid,
                              request.gas_case_id, row.draft_id, row.message_id, row.thread_id,
                              "Recovered by exact shared draft key")
    return None


class SharedDraftCreationAPI:
    """Framework-neutral request handler used by future GAS and Clinic Lead adapters."""

    def __init__(self, *, store: DraftLeaseStore,
                 inspect_mailbox: Callable[[DraftApiRequest], MailboxSnapshot],
                 create_gmail_draft: Callable[[DraftApiRequest], tuple[str, str, str]],
                 lease_seconds: int = 90):
        self.store = store
        self.inspect_mailbox = inspect_mailbox
        self.create_gmail_draft = create_gmail_draft
        self.lease_seconds = max(15, int(lease_seconds))

    def create_or_recover(self, request: DraftApiRequest) -> DraftApiResult:
        error = request.validate()
        if error:
            return DraftApiResult("REJECTED", request.operation_key, request.clinic_uuid,
                                  request.gas_case_id, reason=error)
        owner = str(uuid.uuid4())
        try:
            lease = self.store.acquire(operation_key=request.operation_key,
                                       clinic_uuid=request.clinic_uuid, clinic_id=request.clinic_id,
                                       campaign_id=request.campaign_id, gas_case_id=request.gas_case_id,
                                       owner_token=owner,
                                       lease_seconds=self.lease_seconds)
        except Exception:
            return DraftApiResult("HOLD", request.operation_key, request.clinic_uuid,
                                  request.gas_case_id, reason="Atomic reservation unavailable")
        try:
            state = self.inspect_mailbox(request)
        except Exception:
            self.store.mark_unknown(operation_key=request.operation_key, owner_token=owner,
                                    reason="Gmail inspection failed")
            return DraftApiResult("HOLD", request.operation_key, request.clinic_uuid,
                                  request.gas_case_id, reason="Gmail state could not be verified")
        existing = _result_from_mail_state(request, state)
        if existing:
            observed_status = {
                "DRAFT_RECOVERED": "DRAFT", "SCHEDULED_EXISTS": "SCHEDULED",
                "SENT_EXISTS": "SENT", "TRASHED_EXISTS": "TRASHED",
                "REPLIED_EXISTS": "REPLIED",
            }.get(existing.status)
            if observed_status:
                try:
                    observer = getattr(self.store, "observe_existing", None)
                    if observer:
                        reconciled = observer(
                            operation_key=request.operation_key, clinic_uuid=request.clinic_uuid,
                            clinic_id=request.clinic_id, campaign_id=request.campaign_id,
                            status=observed_status, draft_id=existing.draft_id,
                            message_id=existing.message_id, thread_id=existing.thread_id)
                    elif observed_status == "DRAFT":
                        reconciled = self.store.reconcile_existing(
                            operation_key=request.operation_key, clinic_uuid=request.clinic_uuid,
                            clinic_id=request.clinic_id, campaign_id=request.campaign_id,
                            draft_id=existing.draft_id, message_id=existing.message_id,
                            thread_id=existing.thread_id)
                    else:
                        reconciled = True  # Legacy test stores have no state observer.
                    if not reconciled:
                        raise RuntimeError("reconciliation conflict")
                except Exception:
                    return DraftApiResult("HOLD", request.operation_key, request.clinic_uuid,
                                          request.gas_case_id, existing.draft_id,
                                          existing.message_id, existing.thread_id,
                                          "Draft exists but DB reconciliation failed")
            return existing
        if not lease.acquired:
            if lease.status in {"CROSS_CAMPAIGN_HOLD", "IDENTITY_CONFLICT", "DRAFT", "SCHEDULED",
                                "SENT", "REPLIED", "TRASHED", "UNKNOWN", "HOLD",
                                "OPERATION_KEY_CONFLICT"}:
                return DraftApiResult("HOLD", request.operation_key, request.clinic_uuid,
                                      request.gas_case_id, lease.draft_id,
                                      reason=f"Existing shared reservation blocks creation: {lease.status}")
            return DraftApiResult("IN_PROGRESS", request.operation_key, request.clinic_uuid,
                                  request.gas_case_id, lease.draft_id,
                                  reason="Another request owns the live lease; retry same operation key")
        if lease.status in {"UNKNOWN", "HOLD", "LEASE_EXPIRED_UNKNOWN"}:
            return DraftApiResult("HOLD", request.operation_key, request.clinic_uuid,
                                  request.gas_case_id, lease.draft_id,
                                  reason="Expired/ambiguous lease was checked against Gmail; manual reconciliation required")
        try:
            ids = self.create_gmail_draft(request)
            if len(ids) != 3 or not ids[0]:
                raise TimeoutError("Gmail create response missing Draft ID")
            draft_id, message_id, thread_id = map(str, ids)
        except Exception:
            # Gmail may have persisted the Draft before the client timed out. Re-read
            # all blocking states; never blindly call drafts.create a second time.
            try:
                recovered = _result_from_mail_state(request, self.inspect_mailbox(request))
            except Exception:
                recovered = None
            if recovered and recovered.status == "DRAFT_RECOVERED":
                try:
                    reconciled = self.store.reconcile_existing(
                        operation_key=request.operation_key, clinic_uuid=request.clinic_uuid,
                        clinic_id=request.clinic_id, campaign_id=request.campaign_id,
                        draft_id=recovered.draft_id, message_id=recovered.message_id,
                        thread_id=recovered.thread_id)
                    if not reconciled:
                        raise RuntimeError("reconciliation conflict")
                    return recovered
                except Exception:
                    pass
            self.store.mark_unknown(operation_key=request.operation_key, owner_token=owner,
                                    reason="Gmail create outcome ambiguous")
            return DraftApiResult("HOLD", request.operation_key, request.clinic_uuid,
                                  request.gas_case_id, reason="Create outcome ambiguous; no retry create")
        try:
            completed = self.store.complete(operation_key=request.operation_key, owner_token=owner,
                                            draft_id=draft_id, message_id=message_id,
                                            thread_id=thread_id, fencing_token=lease.fencing_token)
            if not completed:
                return DraftApiResult("HOLD", request.operation_key, request.clinic_uuid,
                                      request.gas_case_id, draft_id, message_id, thread_id,
                                      "Lease fence lost after Gmail create; reconciliation required")
        except Exception:
            # The operation row remains reserved. Retry path will inspect Gmail first.
            return DraftApiResult("HOLD", request.operation_key, request.clinic_uuid,
                                  request.gas_case_id, draft_id, message_id, thread_id,
                                  "Draft created but DB completion failed; retry same operation key")
        return DraftApiResult("DRAFT_CREATED", request.operation_key, request.clinic_uuid,
                              request.gas_case_id, draft_id, message_id, thread_id)
