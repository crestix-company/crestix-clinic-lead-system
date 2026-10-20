"""Postgres adapter for the approved shared Draft lease SQL contract.

Construction requires an injected connection. This module never connects on its own,
so local tests use a fake connection and cannot reach Supabase.
"""
from __future__ import annotations

from typing import Any

from src.outreach.draft_creation_api import DraftLease


class SharedDraftLeaseError(RuntimeError):
    """Sanitized shared-reservation database error."""


class SharedDraftLeaseRepository:
    def __init__(self, connection: Any):
        self._connection = connection

    def acquire(self, *, operation_key: str, clinic_uuid: str, clinic_id: int,
                campaign_id: str, gas_case_id: str, owner_token: str,
                lease_seconds: int) -> DraftLease:
        try:
            with self._connection.transaction():
                with self._connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT acquired,status,fencing_token,gmail_draft_id,lease_expires_at "
                        "FROM outreach.acquire_shared_draft_lease(%s,%s,%s,%s,%s,%s::uuid,%s)",
                        (operation_key, clinic_uuid, int(clinic_id), campaign_id, gas_case_id, owner_token,
                         int(lease_seconds)),
                    )
                    row = cursor.fetchone()
            if not row:
                raise SharedDraftLeaseError("Atomic reservation returned no result")
            acquired, status, fencing_token, draft_id, expires = row
            return DraftLease(operation_key, owner_token if acquired else "", expires,
                              bool(acquired), str(status), str(draft_id or ""), int(fencing_token))
        except SharedDraftLeaseError:
            raise
        except Exception:
            raise SharedDraftLeaseError("Atomic reservation failed") from None

    def complete(self, *, operation_key: str, owner_token: str, draft_id: str,
                 message_id: str, thread_id: str, fencing_token: int) -> bool:
        try:
            with self._connection.transaction():
                with self._connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT outreach.complete_shared_draft_operation(%s,%s::uuid,%s,%s,%s,%s)",
                        (operation_key, owner_token, int(fencing_token), draft_id, message_id, thread_id),
                    )
                    row = cursor.fetchone()
            return bool(row and row[0])
        except Exception:
            raise SharedDraftLeaseError("Draft completion failed") from None

    def reconcile_existing(self, *, operation_key: str, clinic_uuid: str, clinic_id: int,
                           campaign_id: str, draft_id: str, message_id: str,
                           thread_id: str) -> bool:
        return self.observe_existing(operation_key=operation_key, clinic_uuid=clinic_uuid,
                                     clinic_id=clinic_id, campaign_id=campaign_id, status="DRAFT",
                                     draft_id=draft_id, message_id=message_id, thread_id=thread_id)

    def observe_existing(self, *, operation_key: str, clinic_uuid: str, clinic_id: int,
                         campaign_id: str, status: str, draft_id: str,
                         message_id: str, thread_id: str) -> bool:
        return self._observe(operation_key=operation_key, clinic_uuid=clinic_uuid,
                             clinic_id=clinic_id, campaign_id=campaign_id, status=status,
                             draft_id=draft_id, message_id=message_id, thread_id=thread_id)

    def mark_unknown(self, *, operation_key: str, owner_token: str, reason: str) -> None:
        # Do not let an unfenced stale worker overwrite a newer Gmail observation.
        # The lease expiry path atomically changes PENDING to UNKNOWN and fences it.
        return None

    def _observe(self, *, operation_key: str, clinic_uuid: str, clinic_id: int,
                 campaign_id: str, status: str, draft_id: str,
                 message_id: str, thread_id: str) -> bool:
        try:
            with self._connection.transaction():
                with self._connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT outreach.observe_shared_draft_state(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (operation_key, clinic_uuid, int(clinic_id), campaign_id, status,
                         operation_key, draft_id, message_id, thread_id),
                    )
                    row = cursor.fetchone()
            return bool(row and row[0])
        except Exception:
            raise SharedDraftLeaseError("Gmail state reconciliation failed") from None
