"""Pure, side-effect-free contract for migrating GAS sheet cases into Clinic Lead.

No network, database, Gmail, Sheets, or GAS writes. Integration must explicitly
supply verified identities and an existing history snapshot before registration.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import re
import unicodedata
from typing import Mapping, Sequence


UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)


class Decision(str, Enum):
    MATCHED = "MATCHED"
    REVIEW = "REVIEW"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class ClinicIdentity:
    clinic_uuid: str
    clinic_name: str
    director_name: str = ""
    email: str = ""
    phone: str = ""
    address: str = ""


@dataclass(frozen=True)
class LegacyCase:
    legacy_case_id: str
    clinic_name: str
    director_name: str = ""
    email: str = ""
    phone: str = ""
    address: str = ""
    gmail_draft_id: str = ""
    gmail_message_id: str = ""
    gmail_thread_id: str = ""


@dataclass(frozen=True)
class MatchResult:
    decision: Decision
    clinic_uuid: str = ""
    reason: str = ""


def _text(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value or "").casefold().split())


def _phone(value: str) -> str:
    return re.sub(r"[^0-9]", "", value or "")


def _email(value: str) -> str:
    return (value or "").strip().casefold()


def resolve_clinic(case: LegacyCase, candidates: Sequence[ClinicIdentity]) -> MatchResult:
    """Conservative 1:1 match. Names alone and emails alone are never enough.

    Duplicate candidate matches, contradictory details, and incomplete evidence
    always require human review. Does not guess UUIDs.
    """
    if not case.clinic_name.strip():
        return MatchResult(Decision.BLOCKED, reason="legacy clinic name missing")
    possible = []
    for candidate in candidates:
        if not UUID_PATTERN.fullmatch(candidate.clinic_uuid):
            continue
        if _text(case.clinic_name) != _text(candidate.clinic_name):
            continue
        phone_match = bool(_phone(case.phone) and _phone(case.phone) == _phone(candidate.phone))
        address_match = bool(_text(case.address) and _text(case.address) == _text(candidate.address))
        if not (phone_match or address_match):
            continue
        # Any supplied conflicting contact/detail evidence prevents auto-confirmation.
        if case.phone and candidate.phone and _phone(case.phone) != _phone(candidate.phone):
            continue
        if case.address and candidate.address and _text(case.address) != _text(candidate.address):
            continue
        if case.director_name and candidate.director_name and _text(case.director_name) != _text(candidate.director_name):
            continue
        if case.email and candidate.email and _email(case.email) != _email(candidate.email):
            continue
        possible.append(candidate.clinic_uuid)
    if len(possible) == 1:
        return MatchResult(Decision.MATCHED, possible[0], "name + verified phone/address; no conflicting evidence")
    if len(possible) > 1:
        return MatchResult(Decision.REVIEW, reason="multiple matching Clinic UUID candidates")
    return MatchResult(Decision.REVIEW, reason="no uniquely verified clinic; manual review required")


def registration_key(*, clinic_uuid: str, campaign_id: str, recipient_email: str, template_kind: str) -> str:
    """Stable operation key, shared by sheet/Gmail/DB adapters.

    Template and campaign are included to avoid blocking legitimate future
    outreach to the same clinic. This key is NOT a Gmail draft identifier.
    """
    if not UUID_PATTERN.fullmatch(clinic_uuid):
        raise ValueError("verified Clinic UUID required")
    campaign = (campaign_id or "").strip()
    kind = (template_kind or "").strip().upper()
    recipient = _email(recipient_email)
    if not campaign or not recipient or "@" not in recipient or kind not in {"LISTED", "NOT_LISTED"}:
        raise ValueError("campaign, recipient, and supported template required")
    payload = json.dumps([clinic_uuid.lower(), campaign, recipient, kind], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RegistrationPlan:
    decision: Decision
    operation_key: str = ""
    legacy_case_id: str = ""
    reason: str = ""


def plan_registration(*, clinic_uuid: str, campaign_id: str, recipient_email: str,
                      template_kind: str, existing_keys: Sequence[str],
                      history_verified: bool, legacy_case_id: str = "") -> RegistrationPlan:
    """No registration is allowed while the existing GAS/Gmail history is unknown."""
    if not history_verified:
        return RegistrationPlan(Decision.BLOCKED, reason="GAS, sheet and Gmail history not verified")
    try:
        key = registration_key(clinic_uuid=clinic_uuid, campaign_id=campaign_id,
                               recipient_email=recipient_email, template_kind=template_kind)
    except ValueError as exc:
        return RegistrationPlan(Decision.BLOCKED, reason=str(exc))
    if key in set(existing_keys):
        return RegistrationPlan(Decision.REVIEW, operation_key=key,
                                legacy_case_id=legacy_case_id,
                                reason="existing campaign/recipient/template record; reuse or review")
    return RegistrationPlan(Decision.MATCHED, operation_key=key,
                            legacy_case_id=legacy_case_id,
                            reason="eligible for controlled GAS registration after authorization")
