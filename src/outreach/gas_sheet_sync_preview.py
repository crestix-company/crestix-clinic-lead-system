"""Non-writing Sheets projection preview for GAS/Clinic Lead history."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from src.outreach.gas_readonly_inventory import Reconciliation


@dataclass(frozen=True)
class SheetSyncPreview:
    action: str
    source_key: str
    clinic_uuid: str = ""
    gas_case_id: str = ""
    operation_key: str = ""
    draft_id: str = ""
    message_id: str = ""
    thread_id: str = ""
    gmail_status: str = "UNKNOWN"
    changed_fields: tuple[str, ...] = ()
    reason: str = ""


def build_sheet_sync_preview(
    records: Sequence[Reconciliation], *,
    confirmed_clinic_uuids: Mapping[str, str],
    gmail_verified_source_keys: set[str] | frozenset[str],
    existing_projection: Mapping[str, Mapping[str, str]],
    expected_projection_hashes: Mapping[str, str] | None = None,
    current_projection_hashes: Mapping[str, str] | None = None,
) -> tuple[SheetSyncPreview, ...]:
    """Plan INSERT/UPDATE/NOOP/HOLD without touching Sheets.

    A unique, previously confirmed UUID and a verified Gmail observation are required
    before a projection can be written. Existing UUID conflicts and ambiguous source
    rows always HOLD. Keys are source-row keys, so separate sends remain separate rows.
    """
    output = []
    for row in records:
        source_key = row.source_key
        confirmed = str(confirmed_clinic_uuids.get(source_key, "") or "").strip().casefold()
        if (not source_key or not confirmed
                or (bool(row.clinic_uuid.strip()) and confirmed != row.clinic_uuid.strip().casefold())
                or source_key not in gmail_verified_source_keys or row.gmail_status == "UNKNOWN"
                or "IDENTIFIER_CONFLICT" in row.duplicate_kinds):
            reasons = list(row.reasons)
            if not confirmed:
                reasons.append("Clinic UUID未確認")
            elif row.clinic_uuid.strip() and confirmed != row.clinic_uuid.strip().casefold():
                reasons.append("既存/確認済みClinic UUIDと候補が不一致")
            if source_key not in gmail_verified_source_keys:
                reasons.append("Gmail状態未検証")
            output.append(SheetSyncPreview("HOLD", source_key, confirmed or row.clinic_uuid,
                                           row.case_id, row.operation_key, row.draft_id,
                                           row.message_id, row.sent_thread_id, row.gmail_status,
                                           reason="; ".join(dict.fromkeys(reasons))))
            continue

        desired = {
            "clinic_uuid": confirmed,
            "gas_case_id": row.case_id,
            "operation_key": row.operation_key,
            "gmail_draft_id": row.draft_id,
            "gmail_message_id": row.message_id,
            "gmail_thread_id": row.sent_thread_id or row.draft_thread_id,
            "gmail_status": row.gmail_status,
            "source_tab": row.tab,
            "source_row_number": str(row.row_number),
        }
        current = existing_projection.get(source_key)
        expected_hashes = expected_projection_hashes or {}
        actual_hashes = current_projection_hashes or {}
        if (current is not None and source_key in expected_hashes
                and actual_hashes.get(source_key) != expected_hashes[source_key]):
            output.append(SheetSyncPreview("HOLD", source_key, confirmed, row.case_id,
                                           row.operation_key, row.draft_id, row.message_id,
                                           row.sent_thread_id or row.draft_thread_id, row.gmail_status,
                                           reason="preview後にSheets投影行が変更されたため再previewが必要"))
            continue
        if current is None:
            action, changed, reason = "INSERT", tuple(desired), "新規source row projection"
        elif str(current.get("clinic_uuid", "")).strip().casefold() != confirmed:
            action, changed, reason = "HOLD", (), "投影済みClinic UUIDと異なるため上書き禁止"
        else:
            changed = tuple(sorted(key for key, value in desired.items()
                                   if str(current.get(key, "")) != value))
            action, reason = ("UPDATE", "確認済み値に差分あり") if changed else ("NOOP", "差分なし")
        output.append(SheetSyncPreview(action, source_key, confirmed, row.case_id,
                                       row.operation_key, row.draft_id, row.message_id,
                                       row.sent_thread_id or row.draft_thread_id, row.gmail_status,
                                       changed, reason))
    return tuple(output)
