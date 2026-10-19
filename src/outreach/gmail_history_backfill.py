"""Fail-closed, Clinic-ID keyed Gmail history backfill planning.

The default command is a local CSV dry-run. It never connects to Gmail or Postgres.
The apply adapter is deliberately explicit and is not invoked by the CLI.
"""
from __future__ import annotations

import csv
import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

BACKFILL_CAMPAIGN = "gmail-history-backfill-v1"
BLOCKING = {"DRAFT", "SCHEDULED", "SENT", "REPLIED", "TRASHED", "UNKNOWN", "REVIEW"}


@dataclass(frozen=True)
class PlannedClinic:
    clinic_id: int
    clinic_uuid: str
    disposition: str
    history_status: str
    reason: str
    draft_ids: tuple[str, ...]
    message_ids: tuple[str, ...]
    thread_ids: tuple[str, ...]


@dataclass(frozen=True)
class PlannedEvent:
    clinic_id: int
    status: str
    gmail_kind: str
    gmail_id: str
    message_id: str
    thread_id: str
    evidence: str


def planned_events(row: dict[str, str]) -> tuple[PlannedEvent, ...]:
    """Expand only observed Gmail IDs into event records; never invent old IDs."""
    cid = int(row["sales_sheet_clinic_id"])
    events: list[PlannedEvent] = []
    drafts = _ids(row.get("current_draft_id", ""))
    draft_messages = _ids(row.get("current_draft_message_id", ""))
    draft_threads = _ordered_ids(row.get("current_draft_thread_id", ""))
    for i, draft_id in enumerate(drafts):
        events.append(PlannedEvent(cid, "DRAFT", "draft", draft_id,
                                   draft_messages[i] if i < len(draft_messages) else "",
                                   draft_threads[i] if i < len(draft_threads) else "",
                                   "Gmail Draft + underlying message metadata"))
    groups = (
        ("SCHEDULED", "message", _ids(row.get("current_scheduled_message_ids", "")),
         _ordered_ids(row.get("current_scheduled_thread_ids", "")), "Gmail scheduled query"),
        ("SENT", "message", _ids(row.get("current_sent_message_ids", "")),
         _ordered_ids(row.get("current_sent_thread_ids", "")), "Gmail sent query"),
        ("TRASHED", "message", _ids(row.get("current_trashed_message_ids", "")),
         _ordered_ids(row.get("current_trashed_thread_ids", "")), "Gmail Trash query"),
        ("REPLIED", "message", _ids(row.get("reply_message_ids", "")),
         _ordered_ids(row.get("reply_thread_ids", "")), "Gmail reply/thread evidence"),
    )
    for status, kind, ids, threads, evidence in groups:
        for i, gid in enumerate(ids):
            events.append(PlannedEvent(cid, status, kind, gid, gid,
                                       threads[i] if i < len(threads) else "", evidence))
    return tuple(events)


def _ids(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(x.strip() for x in (value or "").split("|") if x.strip()))


def _ordered_ids(value: str) -> tuple[str, ...]:
    """Keep repeated thread IDs aligned with multiple messages in one thread."""
    return tuple(x.strip() for x in (value or "").split("|") if x.strip())


def apply_verified_linkage_overrides(rows: list[dict[str, str]], overrides: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    """Apply explicit, evidence-bearing identity decisions without editing source CSV."""
    import copy
    result = [copy.deepcopy(row) for row in rows]
    by_id = {int(row["sales_sheet_clinic_id"]): row for row in result}
    if len(by_id) != len(result):
        raise ValueError("duplicate Clinic ID in source before overrides")
    seen_ids: set[str] = set()
    for override in overrides:
        cid = int(override["clinic_id"])
        row = by_id.get(cid)
        if row is None:
            raise ValueError(f"override Clinic ID is not in source: {cid}")
        if override.get("verification_status") != "VERIFIED_NAME_AND_EXACT_MASTER_PHONE":
            raise ValueError(f"unsupported identity override for Clinic ID {cid}")
        message_ids = _ids(override.get("sent_message_ids", ""))
        thread_ids = _ordered_ids(override.get("sent_thread_ids", ""))
        if not message_ids or len(message_ids) != len(thread_ids) or not override.get("evidence", "").strip():
            raise ValueError(f"incomplete identity evidence for Clinic ID {cid}")
        for message_id in message_ids:
            if message_id in seen_ids:
                raise ValueError(f"duplicate override Gmail Message ID: {message_id}")
            seen_ids.add(message_id)
        row["current_gmail_state"] = "SENT"
        row["gmail_state_search_result"] = "SENT_VERIFIED_BY_MASTER_PHONE"
        row["current_sent_message_ids"] = "|".join(_ids(row.get("current_sent_message_ids", "")) + message_ids)
        row["current_sent_thread_ids"] = "|".join(_ids(row.get("current_sent_thread_ids", "")) + thread_ids)
        row["identity_review"] = "MATCHED_EXISTING_CLINIC"
        row["backfill_action"] = "BACKFILL_VERIFIED_GMAIL_EVENT"
        row["draft_creation_gate"] = "HOLD_EXISTING_GMAIL_HISTORY"
        row["sales_sheet_confirmation_note"] = (row.get("sales_sheet_confirmation_note", "") +
                                                  " | Gmail identity override: " + override["evidence"]).strip(" |")
    return result


def plan_rows(rows: Iterable[dict[str, str]]) -> list[PlannedClinic]:
    """Use clinic ID as key; never infer identity from name or Comdesk state."""
    out = []
    seen_clinic_ids: set[int] = set()
    all_gmail_ids: dict[str, int] = {}
    for row in rows:
        cid = int(row["sales_sheet_clinic_id"])
        if cid in seen_clinic_ids:
            raise ValueError(f"duplicate Clinic ID in input: {cid}")
        seen_clinic_ids.add(cid)
        uuid = (row.get("clinic_uuid") or "").strip()
        state = (row.get("current_gmail_state") or "UNKNOWN").upper()
        result = (row.get("gmail_state_search_result") or "").upper()
        identity = row.get("identity_review", "")
        draft = _ids(row.get("current_draft_id", "")) + _ids(row.get("historical_sheet_draft_id", ""))
        messages = (_ids(row.get("current_draft_message_id", ""))
                    + _ids(row.get("current_scheduled_message_ids", ""))
                    + _ids(row.get("current_sent_message_ids", ""))
                    + _ids(row.get("current_trashed_message_ids", ""))
                    + _ids(row.get("reply_message_ids", "")))
        threads = (_ids(row.get("current_draft_thread_id", ""))
                   + _ids(row.get("historical_sheet_thread_id", ""))
                   + _ids(row.get("current_scheduled_thread_ids", ""))
                   + _ids(row.get("current_sent_thread_ids", ""))
                   + _ids(row.get("current_trashed_thread_ids", ""))
                   + _ids(row.get("reply_thread_ids", "")))
        status = state if state in BLOCKING else "UNKNOWN"
        # SENT|REPLIED means a reply has occurred after the sent message.
        if "REPLIED" in result:
            status = "REPLIED"
        reasons = []
        if identity == "HOLD_POSSIBLE_DUPLICATE_CLINIC_ID":
            reasons.append("same-name other Clinic ID candidates; address/phone/email review required")
        # UUID is not needed to attach observed Gmail events to Clinic ID. Draft
        # generation has a separate UUID requirement in the composition pipeline.
        if status == "UNKNOWN" or "NOT_FOUND" in result or not result:
            status = "UNKNOWN"
            reasons.append("Gmail state unconfirmed; absence of current draft is not proof of no history")
        elif status in BLOCKING:
            reasons.append(f"existing Gmail history status={status}; suppress automatic recreation")
        disposition = "HOLD" if status == "UNKNOWN" or identity == "HOLD_POSSIBLE_DUPLICATE_CLINIC_ID" else ("SKIP" if status in BLOCKING else "READY")
        # Any known history suppresses recreation, including current absent historic Draft IDs.
        if status != "UNKNOWN":
            disposition = "HOLD" if identity == "HOLD_POSSIBLE_DUPLICATE_CLINIC_ID" else "SKIP"
        for kind, ids in (("draft", draft), ("message", messages), ("thread", threads)):
            for gid in ids:
                key = f"{kind}:{gid}"
                owner = all_gmail_ids.setdefault(key, cid)
                if owner != cid:
                    raise ValueError(f"duplicate Gmail {kind} ID {gid}: Clinic IDs {owner} and {cid}")
        out.append(PlannedClinic(cid, uuid, disposition, status, "; ".join(reasons) or "verified no blocking history", tuple(dict.fromkeys(draft)), tuple(dict.fromkeys(messages)), tuple(dict.fromkeys(threads))))
    if len(out) != 328:
        raise ValueError(f"expected 328 input rows, got {len(out)}")
    return out


def write_plan(source: Path, output: Path, *, overrides: Iterable[dict[str, str]] = ()) -> dict[str, int]:
    with source.open(newline="", encoding="utf-8-sig") as f:
        source_rows = list(csv.DictReader(f))
    rows = plan_rows(apply_verified_linkage_overrides(source_rows, overrides))
    duplicate_count = assert_no_duplicate_gmail_ids(rows)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["clinic_id", "clinic_uuid", "disposition", "gmail_status", "reason", "gmail_draft_ids", "gmail_message_ids", "gmail_thread_ids"])
        for r in rows:
            writer.writerow([r.clinic_id, r.clinic_uuid, r.disposition, r.history_status, r.reason, "|".join(r.draft_ids), "|".join(r.message_ids), "|".join(r.thread_ids)])
    return {**{d: sum(r.disposition == d for r in rows) for d in ("READY", "SKIP", "HOLD")},
            "duplicate_gmail_ids": duplicate_count}


def history_operation_id(clinic_id: int, gmail_kind: str, gmail_id: str) -> str:
    """Stable UUID-shaped operation id for idempotent replay of a Gmail event."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL,
                          f"gmail-history-v1|{int(clinic_id)}|{gmail_kind}|{gmail_id.strip()}"))


def assert_no_duplicate_gmail_ids(rows: Iterable[PlannedClinic]) -> int:
    seen: set[tuple[str, str]] = set()
    for row in rows:
        for kind, values in (("draft", row.draft_ids), ("message", row.message_ids), ("thread", row.thread_ids)):
            for value in values:
                key = kind, value
                if key in seen:
                    raise ValueError(f"duplicate Gmail ID: {key}")
                seen.add(key)
    return 0
