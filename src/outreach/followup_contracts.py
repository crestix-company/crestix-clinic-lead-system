"""Contracts for later reply-draft and appointment integrations.

Only interfaces and validation live here. Implementations must use dedicated provider
adapters, event-cycle idempotency, and separate authorization before any external write.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class ReplyDraftRequest:
    operation_key: str
    parent_message_id: str
    thread_id: str
    recipient: str
    subject: str
    body: str
    reply_kind: str

    def valid(self) -> bool:
        return bool(self.operation_key.strip() and self.parent_message_id.strip()
                    and self.thread_id.strip() and self.recipient.strip()
                    and self.subject.strip() and self.body.strip())


@dataclass(frozen=True)
class AppointmentCycleRequest:
    operation_key: str
    cycle_key: str
    starts_at: datetime
    ends_at: datetime
    timezone: str
    fs_owner: str = ""
    meet_requested: bool = True

    def valid(self) -> bool:
        return bool(self.operation_key.strip() and self.cycle_key.strip()
                    and self.timezone.strip() and self.ends_at > self.starts_at)


@dataclass(frozen=True)
class AppointmentCycleResult:
    status: str
    cycle_key: str
    calendar_event_id: str = ""
    meet_url: str = ""
    reason: str = ""


class ReplyDraftProvider(Protocol):
    """Creates a reply draft in an existing thread. It must not send or schedule."""
    def create_reply_draft(self, request: ReplyDraftRequest) -> str: ...


class CalendarMeetProvider(Protocol):
    """Idempotently creates/retrieves one appointment cycle and optional Meet link."""
    def ensure_appointment_cycle(self, request: AppointmentCycleRequest) -> AppointmentCycleResult: ...
