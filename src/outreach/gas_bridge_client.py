"""Python client for the GAS Clinic Lead registration bridge.

Implements only the JSON contract documented in
~/ClinicLeadGASIntegration/bridge/docs/python_integration_contract.md.

Explicitly out of scope (reused, not reimplemented):
  - Clinic UUID matching / duplicate-registration policy decisions
    (src/outreach/gas_sheet_bridge.py, PR #94). This client treats
    `operation_key` and `clinic_uuid` as opaque, already-decided values.
  - Gmail draft creation (src/outreach/gmail_drafts.py,
    src/outreach/gmail_workflow.py). This client never creates a draft;
    it only ever sends an `existing_draft_id` that was created elsewhere.

There is no deployed GAS Web App yet (see
~/ClinicLeadGASIntegration/bridge/docs/connection_auth_design.md). This
module is transport-agnostic (see `gas_bridge_transport.py` for the stdlib
HTTP implementation) so it can be fully unit tested without network access.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping, Sequence

UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.IGNORECASE
)
EMAIL_PATTERN = re.compile(r"^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$")

# Mirrors clinicLeadBridgeValidateRequest_'s `operationKey.length < 16` check
# (clinic_lead_gas_bridge.js) so invalid requests fail the same way on both sides.
MIN_OPERATION_KEY_LENGTH = 16


class GasCaseStatus(str, Enum):
    """The five values GAS itself can return, plus client-side-only statuses
    that exist because a network call can fail before GAS ever runs."""

    REGISTERED = "REGISTERED"
    ALREADY_REGISTERED = "ALREADY_REGISTERED"
    PENDING = "PENDING"
    RECOVERED = "RECOVERED"
    REJECTED = "REJECTED"

    # Client-side only. GAS never returns these; they distinguish "GAS
    # answered X" from "we never got a GAS answer at all".
    TRANSPORT_ERROR = "TRANSPORT_ERROR"
    AUTH_ERROR = "AUTH_ERROR"
    INVALID_RESPONSE = "INVALID_RESPONSE"


# Per python_integration_contract.md's "Python側が各statusに対して行うべきこと":
SUCCESS_STATUSES = frozenset({GasCaseStatus.REGISTERED, GasCaseStatus.ALREADY_REGISTERED, GasCaseStatus.RECOVERED})
# PENDING: "一定時間おいて同じリクエストをそのまま再送してよい".
# TRANSPORT_ERROR: GAS may never have seen the request; operationKey-based
# idempotency on the GAS side makes a same-payload retry safe.
RETRYABLE_STATUSES = frozenset({GasCaseStatus.PENDING, GasCaseStatus.TRANSPORT_ERROR})
# REJECTED: "自動リトライしない". AUTH_ERROR/INVALID_RESPONSE need a human/config fix,
# not a blind retry.
TERMINAL_FAILURE_STATUSES = frozenset(
    {GasCaseStatus.REJECTED, GasCaseStatus.AUTH_ERROR, GasCaseStatus.INVALID_RESPONSE}
)


@dataclass(frozen=True)
class ScheduleCandidate:
    """One row of the optional `schedule` array in the request contract."""

    date: str = ""
    start: str = ""
    end: str = ""
    display: str = ""
    available_fs: str = ""

    def to_json(self) -> dict[str, str]:
        return {
            "date": self.date,
            "start": self.start,
            "end": self.end,
            "display": self.display,
            "availableFs": self.available_fs,
        }


@dataclass(frozen=True)
class GasCaseRegistrationRequest:
    """One Clinic Lead System -> GAS registration request.

    `operation_key` and `clinic_uuid` must already be decided by
    `gas_sheet_bridge.plan_registration()` / `resolve_clinic()` (PR #94);
    this class does not re-derive or re-validate that policy decision, only
    the wire format. `existing_draft_id` must reference a draft already
    created via `gmail_drafts.create_approved_draft()` / `gmail_workflow.py`.
    """

    operation_key: str
    clinic_uuid: str
    clinic_name: str
    recipient_email: str
    existing_draft_id: str
    clinic_id: str = ""
    director_name: str = ""
    is_name: str = ""
    fs_name: str = ""
    schedule: Sequence[ScheduleCandidate] = field(default_factory=tuple)
    stale_reservation_ms: int | None = None

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "operationKey": self.operation_key,
            "clinicUuid": self.clinic_uuid,
            "clinicId": self.clinic_id,
            "clinicName": self.clinic_name,
            "directorName": self.director_name,
            "recipientEmail": self.recipient_email,
            "isName": self.is_name,
            "fsName": self.fs_name,
            "existingDraft": {"id": self.existing_draft_id},
        }
        if self.schedule:
            payload["schedule"] = [candidate.to_json() for candidate in self.schedule]
        if self.stale_reservation_ms is not None:
            payload["staleReservationMs"] = self.stale_reservation_ms
        return payload


def validate_request(request: GasCaseRegistrationRequest) -> list[str]:
    """Wire-format checks only, mirroring `clinicLeadBridgeValidateRequest_`
    (GAS side). Does not re-implement Clinic UUID matching or
    duplicate-registration policy -- only whether this payload matches the
    documented contract shape well enough to be worth sending.
    """
    errors: list[str] = []

    if not request.operation_key or len(request.operation_key.strip()) < MIN_OPERATION_KEY_LENGTH:
        errors.append("operation_key is missing or too short (opaque key from gas_sheet_bridge.registration_key() required)")

    if not UUID_PATTERN.match(request.clinic_uuid or ""):
        errors.append("clinic_uuid must be a verified UUID from gas_sheet_bridge.resolve_clinic()")

    if not EMAIL_PATTERN.match(request.recipient_email or ""):
        errors.append("recipient_email is missing or invalid")

    if not (request.clinic_name or "").strip():
        errors.append("clinic_name is required")

    if not (request.existing_draft_id or "").strip():
        errors.append(
            "existing_draft_id is required; this client never creates a Gmail draft "
            "(use gmail_drafts.create_approved_draft() first)"
        )

    return errors


@dataclass(frozen=True)
class GasCaseRegistrationResult:
    status: GasCaseStatus
    case_id: str = ""
    draft_id: str = ""
    thread_id: str = ""
    tracking_token: str = ""
    reason: str = ""

    @property
    def is_success(self) -> bool:
        return self.status in SUCCESS_STATUSES

    @property
    def is_retryable(self) -> bool:
        return self.status in RETRYABLE_STATUSES

    @property
    def is_terminal_failure(self) -> bool:
        return self.status in TERMINAL_FAILURE_STATUSES


def parse_response(payload: Mapping[str, Any]) -> GasCaseRegistrationResult:
    raw_status = str(payload.get("status", "")).strip().upper()
    try:
        status = GasCaseStatus(raw_status)
    except ValueError:
        return GasCaseRegistrationResult(
            status=GasCaseStatus.INVALID_RESPONSE,
            reason=f"unrecognized status in GAS response: {raw_status!r}",
        )
    return GasCaseRegistrationResult(
        status=status,
        case_id=str(payload.get("caseId", "") or ""),
        draft_id=str(payload.get("draftId", "") or ""),
        thread_id=str(payload.get("threadId", "") or ""),
        tracking_token=str(payload.get("trackingToken", "") or ""),
        reason=str(payload.get("reason", "") or ""),
    )


class GasBridgeTransportError(RuntimeError):
    """Raised by a transport implementation. Must never carry credential material."""


# A transport is any callable: (payload: dict, *, timeout_seconds: float) -> (status_code, body_text)
GasBridgeTransport = Callable[..., tuple[int, str]]


class GasBridgeClient:
    """Sends registration requests to the GAS Clinic Lead bridge.

    `transport` is injected for testability. In production it wraps an HTTPS
    POST (see `gas_bridge_transport.py`); in tests it is a mock with no
    network access.
    """

    def __init__(self, transport: GasBridgeTransport, *, default_timeout_seconds: float = 15.0) -> None:
        self._transport = transport
        self._default_timeout_seconds = default_timeout_seconds

    def register_case(
        self, request: GasCaseRegistrationRequest, *, timeout_seconds: float | None = None
    ) -> GasCaseRegistrationResult:
        validation_errors = validate_request(request)
        if validation_errors:
            # Fail closed before touching the network -- mirrors the GAS side's
            # own behaviour of rejecting without touching Sheets/Gmail.
            return GasCaseRegistrationResult(status=GasCaseStatus.REJECTED, reason="; ".join(validation_errors))

        payload = request.to_json()
        timeout = timeout_seconds if timeout_seconds is not None else self._default_timeout_seconds

        try:
            status_code, body = self._transport(payload, timeout_seconds=timeout)
        except GasBridgeTransportError:
            return GasCaseRegistrationResult(status=GasCaseStatus.TRANSPORT_ERROR, reason="transport failed")
        except Exception as error:  # noqa: BLE001 - transport implementations raise varied errors (timeouts, DNS, etc.)
            return GasCaseRegistrationResult(
                status=GasCaseStatus.TRANSPORT_ERROR, reason=f"transport failed ({type(error).__name__})"
            )

        if status_code in (401, 403):
            return GasCaseRegistrationResult(status=GasCaseStatus.AUTH_ERROR, reason=f"HTTP {status_code}")
        if status_code != 200:
            return GasCaseRegistrationResult(status=GasCaseStatus.TRANSPORT_ERROR, reason=f"unexpected HTTP {status_code}")

        try:
            parsed = json.loads(body)
        except (TypeError, ValueError) as error:
            return GasCaseRegistrationResult(
                status=GasCaseStatus.INVALID_RESPONSE, reason=f"response was not valid JSON: {error}"
            )

        if not isinstance(parsed, dict):
            return GasCaseRegistrationResult(status=GasCaseStatus.INVALID_RESPONSE, reason="response JSON was not an object")

        return parse_response(parsed)


def build_auth_fields(
    body_json: str, shared_secret: str, *, now: float | None = None, nonce: str | None = None
) -> dict[str, str]:
    """Return the body-envelope authentication fields shared with GAS.

    The signed bytes are ``timestamp + LF + nonce + LF + payload`` where
    payload is the exact compact, recursively key-sorted JSON string.
    The secret is used only to calculate the signature and is never returned.
    """
    if not shared_secret:
        raise ValueError("shared_secret is required to build auth fields")
    timestamp = str(int(now if now is not None else time.time()))
    request_nonce = nonce or secrets.token_urlsafe(18)
    if not re.fullmatch(r"[A-Za-z0-9_-]{24,}", request_nonce):
        raise ValueError("nonce must contain at least 24 URL-safe characters")
    if not re.fullmatch(r"\d{10}", timestamp):
        raise ValueError("timestamp must be a 10-digit Unix timestamp")
    signed_payload = f"{timestamp}\n{request_nonce}\n{body_json}".encode("utf-8")
    signature = hmac.new(shared_secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return {"timestamp": timestamp, "nonce": request_nonce, "signature": signature}
