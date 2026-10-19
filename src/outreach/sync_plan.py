"""Pure conflict-aware sync planning and privacy-minimized click/open event shaping."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping
from urllib.parse import urlsplit


FIELD_AUTHORITIES = {
    "gmail_status": "GMAIL_API",
    "gmail_draft_id": "GMAIL_API",
    "gmail_message_id": "GMAIL_API",
    "gmail_thread_id": "GMAIL_API",
    "is_owner": "GAS_SHEETS",
    "fs_owner": "GAS_SHEETS",
    "form_payload": "GAS_SHEETS",
    "article_selection": "ARTICLE_AUDIT",
    "open_click_event": "TRACKING_API",
    "appointment": "GOOGLE_CALENDAR_AND_GAS",
}


@dataclass(frozen=True)
class SyncPlan:
    updates: dict[str, object]
    conflicts: dict[str, tuple[object, object, str]]
    unchanged: tuple[str, ...]


def plan_field_sync(current: Mapping[str, object], incoming: Mapping[str, object], *,
                    source_system: str, known_sources: Mapping[str, str] | None = None) -> SyncPlan:
    """Never overwrite a differing value from a different source without review."""
    known_sources = known_sources or {}
    updates, conflicts, unchanged = {}, {}, []
    for field, new_value in incoming.items():
        if field not in current or current.get(field) in (None, ""):
            updates[field] = new_value
        elif current[field] == new_value:
            unchanged.append(field)
        elif known_sources.get(field, source_system) not in {source_system, "UNSET"}:
            conflicts[field] = (current[field], new_value, known_sources[field])
        else:
            updates[field] = new_value
    return SyncPlan(updates, conflicts, tuple(unchanged))


def event_idempotency_key(source_system: str, source_event_id: str) -> str:
    if not source_system.strip() or not source_event_id.strip():
        raise ValueError("source and stable source event id are required")
    return hashlib.sha256(f"{source_system.strip()}|{source_event_id.strip()}".encode()).hexdigest()


def shape_tracking_event(*, operation_key: str, event_id: str, event_type: str,
                         url: str = "", occurred_at: datetime | None = None,
                         ip_address: str = "", user_agent: str = "") -> dict:
    kind = event_type.strip().upper()
    if kind not in {"OPEN", "CLICK"}:
        raise ValueError("Only OPEN and CLICK tracking events are accepted")
    if not operation_key.strip() or not event_id.strip():
        raise ValueError("operation_key and provider event id are required")
    if kind == "CLICK":
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("click URL must be HTTPS")
    stamp = occurred_at or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        raise ValueError("event timestamp must include timezone")
    ip_hash = hashlib.sha256(ip_address.strip().encode()).hexdigest() if ip_address.strip() else ""
    ua_hash = hashlib.sha256(user_agent.strip().encode()).hexdigest() if user_agent.strip() else ""
    return {
        "operation_key": operation_key.strip(),
        "source_system": "TRACKING_API",
        "source_event_key": event_idempotency_key("TRACKING_API", event_id),
        "event_type": kind,
        "event_at": stamp.astimezone(timezone.utc).isoformat(),
        "url": url.strip() if kind == "CLICK" else "",
        "privacy_metadata": {"ip_sha256": ip_hash, "user_agent_sha256": ua_hash},
    }


__all__ = ["FIELD_AUTHORITIES", "SyncPlan", "event_idempotency_key", "plan_field_sync", "shape_tracking_event"]
