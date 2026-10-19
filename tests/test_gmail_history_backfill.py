import pytest

from src.outreach.gmail_history_backfill import apply_verified_linkage_overrides, assert_no_duplicate_gmail_ids, history_operation_id, plan_rows, planned_events
from src.repository.outreach_delivery_repository import OutreachDeliveryRepository, OutreachDatabaseError

def synthetic_328_clinic_rows():
    """Generate anonymous cohort-shaped test data without real clinic or Gmail IDs."""
    rows = [
        {"sales_sheet_clinic_id": str(i), "clinic_uuid": "" if i <= 2 else f"uuid-{i}",
         "clinic_master_name": f"Clinic {i}", "current_gmail_state": "UNKNOWN",
         "gmail_state_search_result": "NOT_FOUND", "identity_review": "MATCHED_EXISTING_CLINIC"}
        for i in range(1, 329)
    ]
    for i in range(3, 23):
        rows[i - 1].update(current_gmail_state="DRAFT", gmail_state_search_result="DRAFT",
                           current_draft_id=f"draft-{i}", current_draft_message_id=f"draft-message-{i}",
                           current_draft_thread_id=f"thread-{i}")
    for i in range(23, 95):
        rows[i - 1].update(current_gmail_state="SCHEDULED", gmail_state_search_result="SCHEDULED",
                           current_scheduled_message_ids=f"scheduled-{i}",
                           current_scheduled_thread_ids=f"thread-{i}")
    # Twenty-two SENT events across 21 clinics; one clinic also has a reply event.
    for i in range(95, 115):
        rows[i - 1].update(current_gmail_state="SENT", gmail_state_search_result="SENT",
                           current_sent_message_ids=f"sent-{i}", current_sent_thread_ids=f"thread-{i}")
    rows[114].update(current_gmail_state="SENT", gmail_state_search_result="SENT",
                     current_sent_message_ids="sent-115a|sent-115b",
                     current_sent_thread_ids="thread-115|thread-115")
    rows[94]["reply_message_ids"] = "reply-95"
    rows[94]["reply_thread_ids"] = "thread-95"
    rows[115].update(current_gmail_state="TRASHED", gmail_state_search_result="TRASHED",
                     current_trashed_message_ids="trash-116", current_trashed_thread_ids="thread-116")
    # Keep 31 observed-history clinics on HOLD to model ambiguous duplicate-ID review.
    for row in rows[83:114]:
        row["identity_review"] = "HOLD_POSSIBLE_DUPLICATE_CLINIC_ID"
    return rows


def test_anonymous_328_cohort_aggregation_is_unique_and_fails_closed():
    source_rows = synthetic_328_clinic_rows()
    rows = plan_rows(source_rows)
    assert len(rows) == 328
    assert assert_no_duplicate_gmail_ids(rows) == 0
    assert sum(r.disposition == "READY" for r in rows) == 0
    assert sum(r.disposition == "HOLD" for r in rows) == 245
    assert sum(r.disposition == "SKIP" for r in rows) == 83
    uuid_missing = {r.clinic_id for r in rows if not r.clinic_uuid}
    assert len(uuid_missing) == 2
    assert all(r.disposition == "HOLD" for r in rows if r.clinic_id in uuid_missing)
    assert all("UUID" not in r.reason for r in rows if r.clinic_id in uuid_missing)
    events = [event for row in source_rows for event in planned_events(row)]
    assert len(events) == 116  # Draft ID + underlying message ID are one event.
    keys = [(event.clinic_id, event.gmail_kind, event.gmail_id) for event in events]
    assert len(keys) == len(set(keys))
    assert {event.clinic_id for event in events} == {r.clinic_id for r in rows if r.disposition != "READY" and r.history_status != "UNKNOWN"}
    assert len({history_operation_id(*key) for key in keys}) == len(keys)
    sink = {}
    for _run in range(2):
        for event in events:
            key = (event.clinic_id, event.gmail_kind, event.gmail_id)
            sink.setdefault(key, (history_operation_id(*key), event.status, event.message_id, event.thread_id))
    assert len(sink) == 116
    assert all(value[0] == history_operation_id(*key) for key, value in sink.items())


def test_duplicate_clinic_id_rejected():
    row = {"sales_sheet_clinic_id": "1", "current_gmail_state": "UNKNOWN",
           "gmail_state_search_result": "NOT_FOUND", "identity_review": "MATCHED_EXISTING_CLINIC"}
    with pytest.raises(ValueError, match="duplicate Clinic ID"):
        plan_rows([row, row])


def test_same_gmail_draft_id_across_clinics_rejected():
    rows = [
        {"sales_sheet_clinic_id": "1", "clinic_uuid": "u1", "current_gmail_state": "DRAFT",
         "gmail_state_search_result": "DRAFT", "identity_review": "MATCHED_EXISTING_CLINIC", "current_draft_id": "same"},
        {"sales_sheet_clinic_id": "2", "clinic_uuid": "u2", "current_gmail_state": "DRAFT",
         "gmail_state_search_result": "DRAFT", "identity_review": "MATCHED_EXISTING_CLINIC", "current_draft_id": "same"},
    ]
    with pytest.raises(ValueError, match="duplicate Gmail draft ID"):
        plan_rows(rows)


def test_event_expansion_keeps_observed_states_and_never_promotes_unfound_historic_id():
    row = {"sales_sheet_clinic_id": "41", "current_draft_id": "d1",
           "current_draft_message_id": "m1", "current_draft_thread_id": "t1",
           "current_scheduled_message_ids": "m2", "current_scheduled_thread_ids": "t2",
           "current_sent_message_ids": "m3", "current_sent_thread_ids": "t3",
           "current_trashed_message_ids": "m4", "current_trashed_thread_ids": "t4",
           "reply_message_ids": "m5", "reply_thread_ids": "t3",
           "historical_sheet_draft_id": "gone", "historical_draft_id_currently_found": "false"}
    events = planned_events(row)
    assert [(e.status, e.gmail_id) for e in events] == [
        ("DRAFT", "d1"), ("SCHEDULED", "m2"),
        ("SENT", "m3"), ("TRASHED", "m4"), ("REPLIED", "m5")]
    assert events[0].message_id == "m1"
    assert all(e.gmail_id != "gone" for e in events)
    assert history_operation_id(41, "draft", "d1") == history_operation_id(41, "draft", "d1")


def test_verified_phone_name_override_links_only_existing_clinic_id_and_adds_sent_events():
    rows = synthetic_328_clinic_rows()
    rows[199].update(current_gmail_state="UNKNOWN", gmail_state_search_result="NOT_FOUND",
                     identity_review="HOLD_POSSIBLE_DUPLICATE_CLINIC_ID")
    overrides = [{"clinic_id": "200", "verification_status": "VERIFIED_NAME_AND_EXACT_MASTER_PHONE",
                  "sent_message_ids": "verified-message-a|verified-message-b",
                  "sent_thread_ids": "verified-thread|verified-thread",
                  "evidence": "synthetic test only: clinic name and phone match"}]
    updated = apply_verified_linkage_overrides(rows, overrides)
    plan = {r.clinic_id: r for r in plan_rows(updated)}
    assert plan[200].disposition == "SKIP"
    assert plan[200].history_status == "SENT"
    assert plan[200].clinic_uuid == "uuid-200"
    clinic_events = [e for row in updated if row["sales_sheet_clinic_id"] == "200" for e in planned_events(row)]
    assert [e.gmail_id for e in clinic_events] == ["verified-message-a", "verified-message-b"]
    assert len({e.thread_id for e in clinic_events}) == 1


def test_verified_override_reclassification_is_0_ready_83_skip_245_hold_and_event_keys_are_unique():
    rows = synthetic_328_clinic_rows()
    rows[199].update(current_gmail_state="UNKNOWN", gmail_state_search_result="NOT_FOUND",
                     identity_review="HOLD_POSSIBLE_DUPLICATE_CLINIC_ID")
    overrides = [{"clinic_id": "200", "verification_status": "VERIFIED_NAME_AND_EXACT_MASTER_PHONE",
                  "sent_message_ids": "verified-message-a|verified-message-b",
                  "sent_thread_ids": "verified-thread|verified-thread",
                  "evidence": "synthetic test only: clinic name and phone match"}]
    updated = apply_verified_linkage_overrides(rows, overrides)
    plan = plan_rows(updated)
    assert {d: sum(r.disposition == d for r in plan) for d in ("READY", "SKIP", "HOLD")} == {
        "READY": 0, "SKIP": 84, "HOLD": 244}
    events = [event for row in updated for event in planned_events(row)]
    keys = [(event.clinic_id, event.gmail_kind, event.gmail_id) for event in events]
    assert len(events) == 118
    assert len(keys) == len(set(keys))


class _FakeCursor:
    def __init__(self, db, guards, cases): self.db = db; self.guards = guards; self.cases = cases; self.result = None; self.rows = []; self.no_result = False
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def execute(self, sql, params=()):
        if "INSERT INTO outreach.clinic_draft_guard" in sql:
            self.guards.add(params[0])
            self.no_result = True
            self.result = None
        elif "INSERT INTO outreach.sales_cases" in sql:
            key, _external, clinic_id, clinic_uuid, campaign_id = params
            self.cases[key] = (f"sales-case:{key}", clinic_id, clinic_uuid, campaign_id)
            self.no_result = False
            self.result = self.cases[key]
        elif "FROM outreach.sales_cases WHERE operation_key" in sql:
            self.no_result = False
            self.result = self.cases.get(params[0])
        elif "INSERT INTO outreach.email_delivery_records" in sql:
            self.no_result = False
            key = params[2]
            existing = self.db.get(key)
            draft_id, message_id = params[16] or "", params[17] or ""
            if existing is None:
                existing = next((v for v in self.db.values()
                                 if (draft_id and v["gmail_draft_id"] == draft_id)
                                 or (message_id and v["gmail_message_id"] == message_id)), None)
            if existing is None:
                existing = {"clinic_id": params[4], "sales_case_id": params[3], "status": params[13], "gmail_draft_id": draft_id,
                            "gmail_message_id": message_id, "gmail_thread_id": params[18] or ""}
                self.db[key] = existing
                self.result = (params[0],)
            else:
                self.result = None
        elif "SELECT clinic_id,status,gmail_draft_id" in sql:
            key, draft_id, message_id = params
            self.rows = [v for k, v in self.db.items() if k == key or (draft_id and v["gmail_draft_id"] == draft_id)
                         or (message_id and v["gmail_message_id"] == message_id)]
            self.result = None
        else:
            self.no_result = False
            self.result = None
    def fetchone(self):
        if self.no_result:
            raise AssertionError("fetchone() called for INSERT without RETURNING")
        value, self.result = self.result, None
        return value
    def fetchall(self): return [tuple(x[k] for k in ("clinic_id", "status", "gmail_draft_id", "gmail_message_id", "gmail_thread_id")) for x in self.rows]


class _FakeConnection:
    def __init__(self): self.rows = {}; self.guards = set(); self.cases = {}
    class _Transaction:
        def __enter__(self): return self
        def __exit__(self, *_): return False
    def transaction(self): return self._Transaction()
    def cursor(self): return _FakeCursor(self.rows, self.guards, self.cases)


def test_repository_backfill_rerun_is_idempotent_and_uses_clinic_id_without_uuid():
    repo = OutreachDeliveryRepository(_FakeConnection())
    fields = dict(clinic_id=500, clinic_uuid=None, clinic_name="Clinic 500", clinic_address="Tokyo",
                  recipient_email="clinic@example.test", sender_account="sender@example.test",
                  gmail_kind="message", gmail_id="sent-msg-500", message_id="sent-msg-500",
                  thread_id="thread-500", status="SENT")
    assert repo.backfill_history_event(**fields) is True
    assert repo.backfill_history_event(**fields) is False
    assert len(repo._conn.rows) == 1
    assert repo._conn.guards == {500}
    row = next(iter(repo._conn.rows.values()))
    assert row["clinic_id"] == 500 and row["gmail_message_id"] == "sent-msg-500"
    assert row["gmail_thread_id"] == "thread-500"
    assert row["sales_case_id"].startswith("sales-case:")


def test_repository_backfill_rejects_same_gmail_id_for_different_clinic_ids():
    repo = OutreachDeliveryRepository(_FakeConnection())
    base = dict(clinic_uuid="u1", clinic_name="Clinic", clinic_address="Tokyo",
                recipient_email="clinic@example.test", sender_account="sender@example.test",
                gmail_kind="message", gmail_id="shared-msg", message_id="shared-msg",
                thread_id="shared-thread", status="SENT")
    repo.backfill_history_event(clinic_id=500, **base)
    with pytest.raises(OutreachDatabaseError, match="collision"):
        repo.backfill_history_event(clinic_id=501, **base)
