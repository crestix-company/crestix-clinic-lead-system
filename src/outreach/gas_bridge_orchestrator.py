"""Thin glue between existing Clinic Lead System modules and GasBridgeClient.

Reuses (does not reimplement):
  - `src.outreach.gas_sheet_bridge` (PR #94): Clinic UUID matching and
    `operation_key` / duplicate-registration policy.
  - `src.outreach.gmail_drafts.create_approved_draft()`: Gmail draft
    creation. This module never calls it and never creates a draft itself --
    callers must have already created one and pass its `draft_id` in.

Left out of scope deliberately (future wiring, not duplicated here):
  - `outreach_delivery_repository.py` lives on a different, unmerged branch
    (`feat/email-outreach-completion-20261017`) owned by another in-progress
    session. This orchestrator's own ledger (`gas_bridge_ledger.py`) is a
    separate, local-only record of GAS registration outcomes; reconciling it
    with that repository is future work, not attempted here.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .gas_bridge_client import (
    GasBridgeClient,
    GasCaseRegistrationRequest,
    GasCaseRegistrationResult,
    ScheduleCandidate,
)
from .gas_bridge_ledger import DEFAULT_LEDGER_PATH, find_by_operation_key, record_result


@dataclass(frozen=True)
class ExistingGmailDraft:
    """Describes a draft Gmail already has for this operation.

    Produced elsewhere (e.g. by `gmail_drafts.create_approved_draft()` /
    `gmail_workflow.py`) -- this orchestrator only ever reads `draft_id`.
    """

    draft_id: str


def register_with_existing_draft(
    client: GasBridgeClient,
    *,
    operation_key: str,
    clinic_uuid: str,
    clinic_name: str,
    recipient_email: str,
    existing_draft: ExistingGmailDraft,
    clinic_id: str = "",
    director_name: str = "",
    is_name: str = "",
    fs_name: str = "",
    schedule: Sequence[ScheduleCandidate] = (),
    ledger_path: Path | None = None,
) -> GasCaseRegistrationResult:
    """Registers a case against GAS using an already-created Gmail draft.

    Draft-ID-first duplicate prevention: if this `operation_key` already has
    a ledger record with a `draft_id`, that recorded draft_id is reused
    verbatim instead of whatever `existing_draft.draft_id` the caller passed
    in for this invocation. This protects a retry from accidentally
    registering a *different* draft under the same operation_key (GAS itself
    also refuses to let a second draft override an already-registered case --
    see `clinic_lead_gas_bridge.js`'s "別のexistingDraft.idを渡しても..." test --
    but catching the mismatch locally, before even calling GAS, surfaces the
    inconsistency sooner).

    GAS is still called on every invocation (not skipped locally) so that
    GAS's own idempotent state (ALREADY_REGISTERED / PENDING / RECOVERED)
    remains the single source of truth; this function never substitutes a
    locally-cached success without confirming it against GAS.
    """
    path = ledger_path or (Path.cwd() / DEFAULT_LEDGER_PATH)
    previous = find_by_operation_key(path, operation_key)
    draft_id = previous.get("draft_id") if previous and previous.get("draft_id") else existing_draft.draft_id

    request = GasCaseRegistrationRequest(
        operation_key=operation_key,
        clinic_uuid=clinic_uuid,
        clinic_name=clinic_name,
        recipient_email=recipient_email,
        existing_draft_id=draft_id,
        clinic_id=clinic_id,
        director_name=director_name,
        is_name=is_name,
        fs_name=fs_name,
        schedule=tuple(schedule),
    )

    result = client.register_case(request)
    record_result(path, operation_key=operation_key, clinic_uuid=clinic_uuid, result=result)
    return result
