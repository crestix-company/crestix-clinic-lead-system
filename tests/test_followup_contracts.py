from datetime import datetime, timedelta, timezone

from src.outreach.followup_contracts import AppointmentCycleRequest, ReplyDraftRequest


def test_reply_draft_requires_parent_message_and_thread_identity():
    req = ReplyDraftRequest("op-1", "message-1", "thread-1", "clinic@example.jp",
                            "Re: Subject", "Draft body", "SCHEDULING")
    assert req.valid()
    assert not ReplyDraftRequest("op-1", "", "thread-1", "clinic@example.jp",
                                 "Re: Subject", "Draft body", "SCHEDULING").valid()


def test_calendar_cycle_is_validated_and_reschedule_uses_distinct_cycle_key():
    start = datetime.now(timezone.utc)
    first = AppointmentCycleRequest("op-1", "cycle-1", start, start + timedelta(hours=1), "UTC")
    second = AppointmentCycleRequest("op-1", "cycle-2", start, start + timedelta(hours=1), "UTC")
    invalid = AppointmentCycleRequest("op-1", "cycle-3", start, start, "UTC")
    assert first.valid() and second.valid()
    assert first.cycle_key != second.cycle_key
    assert not invalid.valid()
