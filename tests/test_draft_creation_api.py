from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Lock

from src.outreach.draft_creation_api import (
    DraftApiRequest, DraftLease, MailboxSnapshot, SharedDraftCreationAPI,
)
from src.outreach.gmail_drafts import ExistingDraft, ExistingMessage
from src.outreach.gmail_workflow import _draft_key

UUID = "123e4567-e89b-42d3-a456-426614174000"


class MemoryLeaseStore:
    def __init__(self):
        self.lock = Lock()
        self.rows = {}
        self.fail_complete_once = False
        self.now = datetime.now(timezone.utc)

    def acquire(self, *, operation_key, clinic_uuid, clinic_id, campaign_id, gas_case_id,
                owner_token, lease_seconds):
        with self.lock:
            row = self.rows.get(operation_key)
            if row and (row["clinic_uuid"] != clinic_uuid or row["clinic_id"] != clinic_id
                        or row["campaign_id"] != campaign_id):
                raise ValueError("operation identity conflict")
            if row and row.get("status") == "LEASE_EXPIRED_UNKNOWN":
                return DraftLease(operation_key, "", row["expires"], False, "UNKNOWN",
                                  row.get("draft_id", ""), row.get("fence", 0))
            if row and row["expires"] > self.now and row.get("owner") != owner_token:
                return DraftLease(operation_key, "", row["expires"], False, "LEASED",
                                  row.get("draft_id", ""), row.get("fence", 0))
            expired_ambiguous = bool(row and row["expires"] <= self.now
                                     and row.get("status") in {"PENDING", "UNKNOWN", "HOLD"})
            if expired_ambiguous:
                row.update(status="LEASE_EXPIRED_UNKNOWN", owner="", expires=self.now)
                row["fence"] = row.get("fence", 0) + 1
                return DraftLease(operation_key, "", row["expires"], False, "UNKNOWN",
                                  row.get("draft_id", ""), row["fence"])
            next_fence = (row.get("fence", 0) + 1) if row else 1
            self.rows[operation_key] = {"clinic_uuid": clinic_uuid, "clinic_id": clinic_id,
                "campaign_id": campaign_id, "owner": owner_token,
                "expires": self.now + timedelta(seconds=lease_seconds),
                "status": "PENDING", "draft_id": "", "fence": next_fence}
            return DraftLease(operation_key, owner_token, self.rows[operation_key]["expires"], True,
                              self.rows[operation_key]["status"], "", next_fence)

    def complete(self, *, operation_key, owner_token, draft_id, message_id, thread_id, fencing_token):
        with self.lock:
            row = self.rows[operation_key]
            if (row["owner"] != owner_token or row["expires"] <= self.now
                    or row["fence"] != fencing_token):
                return False
            if self.fail_complete_once:
                self.fail_complete_once = False
                raise ConnectionError("synthetic DB failure")
            row.update(status="DRAFT", draft_id=draft_id, message_id=message_id, thread_id=thread_id)
            return True

    def reconcile_existing(self, *, operation_key, clinic_uuid, clinic_id, campaign_id,
                           draft_id, message_id, thread_id):
        with self.lock:
            row = self.rows.get(operation_key)
            if not row or (row["clinic_uuid"], row["clinic_id"], row["campaign_id"]) != (
                    clinic_uuid, clinic_id, campaign_id):
                return False
            row.update(status="DRAFT", draft_id=draft_id, message_id=message_id, thread_id=thread_id)
            return True

    def observe_existing(self, *, operation_key, clinic_uuid, clinic_id, campaign_id,
                         status, draft_id, message_id, thread_id):
        with self.lock:
            row = self.rows.get(operation_key)
            if not row or (row["clinic_uuid"], row["clinic_id"], row["campaign_id"]) != (
                    clinic_uuid, clinic_id, campaign_id):
                return False
            row.update(status=status, draft_id=draft_id, message_id=message_id, thread_id=thread_id)
            return True

    def mark_unknown(self, *, operation_key, owner_token, reason):
        with self.lock:
            row = self.rows.get(operation_key)
            if row and row.get("owner") == owner_token:
                row["status"] = "UNKNOWN"


class FakeMailbox:
    def __init__(self):
        self.snapshot = MailboxSnapshot()
        self.create_count = 0
        self.fail_after_persist = False
        self.block = None
        self.lock = Lock()

    def inspect(self, request):
        return self.snapshot

    def create(self, request):
        if self.block:
            self.block.wait(timeout=2)
        with self.lock:
            self.create_count += 1
            draft_id = f"d-{self.create_count}"
            self.snapshot = MailboxSnapshot(drafts=(ExistingDraft(
                draft_id, request.draft_key, (request.recipient.casefold(),), request.subject,
                clinic_id=str(request.clinic_id), clinic_uuid=request.clinic_uuid,
                outreach_id=request.operation_key, campaign_id=request.campaign_id,
                message_id=f"m-{draft_id}", thread_id=f"t-{draft_id}"),))
        if self.fail_after_persist:
            self.fail_after_persist = False
            raise TimeoutError("ambiguous Gmail timeout")
        return draft_id, f"m-{draft_id}", f"t-{draft_id}"


def request(operation="op-000000000000001", campaign="campaign-a", uuid=UUID):
    return DraftApiRequest(operation, uuid, 101, campaign, "clinic@example.jp", "Subject", "Body",
                           identity_verified=True, clinic_name="Clinic", clinic_address="Tokyo",
                           gas_case_id="GAS-20261020-001")


def setup(mailbox=None, store=None):
    mailbox = mailbox or FakeMailbox()
    store = store or MemoryLeaseStore()
    api = SharedDraftCreationAPI(store=store, inspect_mailbox=mailbox.inspect,
                                 create_gmail_draft=mailbox.create, lease_seconds=30)
    return api, store, mailbox


def test_double_click_and_same_operation_reuse_one_draft():
    api, _, mailbox = setup()
    first = api.create_or_recover(request())
    second = api.create_or_recover(request())
    assert first.status == "DRAFT_CREATED"
    assert second.status == "DRAFT_RECOVERED"
    assert mailbox.create_count == 1
    assert request().gmail_headers["X-Clinic-Lead-Draft-Key"] == _draft_key(101, "campaign-a")


def test_gas_and_clinic_lead_concurrent_requests_create_at_most_one_draft():
    api, _, mailbox = setup()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(api.create_or_recover, (request(), request())))
    assert mailbox.create_count == 1
    assert {r.status for r in results} <= {"DRAFT_CREATED", "DRAFT_RECOVERED", "IN_PROGRESS"}


def test_db_failure_after_gmail_success_recovers_on_retry_without_second_create():
    mailbox, store = FakeMailbox(), MemoryLeaseStore()
    store.fail_complete_once = True
    api, _, _ = setup(mailbox, store)
    first = api.create_or_recover(request())
    second = api.create_or_recover(request())
    assert first.status == "HOLD"
    assert second.status == "DRAFT_RECOVERED"
    assert store.rows[request().operation_key]["draft_id"] == "d-1"
    assert mailbox.create_count == 1


def test_gmail_timeout_after_persist_recovers_exact_draft():
    mailbox = FakeMailbox()
    mailbox.fail_after_persist = True
    api, _, _ = setup(mailbox)
    result = api.create_or_recover(request())
    assert result.status == "DRAFT_RECOVERED"
    assert result.draft_id == "d-1"
    assert mailbox.create_count == 1


def test_expired_lease_can_be_reclaimed_and_existing_gmail_draft_is_recovered():
    api, store, mailbox = setup()
    api.create_or_recover(request())
    store.now += timedelta(minutes=5)
    result = api.create_or_recover(request())
    assert result.status == "DRAFT_RECOVERED"
    assert mailbox.create_count == 1


def test_expired_lease_fencing_rejects_old_worker_completion():
    store = MemoryLeaseStore()
    first = store.acquire(operation_key="fenced-op", clinic_uuid=UUID, clinic_id=101,
                         campaign_id="campaign-a", gas_case_id="", owner_token="owner-a", lease_seconds=20)
    store.now += timedelta(seconds=21)
    second = store.acquire(operation_key="fenced-op", clinic_uuid=UUID, clinic_id=101,
                          campaign_id="campaign-a", gas_case_id="", owner_token="owner-b", lease_seconds=20)
    assert first.acquired and not second.acquired and second.status == "UNKNOWN"
    assert not store.complete(operation_key="fenced-op", owner_token="owner-a",
                              draft_id="stale", message_id="", thread_id="",
                              fencing_token=first.fencing_token)


def test_expired_ambiguous_lease_without_gmail_result_does_not_retry_create():
    mailbox, store = FakeMailbox(), MemoryLeaseStore()
    store.rows[request().operation_key] = {"clinic_uuid": UUID, "clinic_id": 101,
        "campaign_id": "campaign-a", "owner": "dead-worker",
        "expires": store.now - timedelta(seconds=1), "status": "PENDING", "draft_id": "", "fence": 3}
    api, _, _ = setup(mailbox, store)
    result = api.create_or_recover(request())
    assert result.status == "HOLD"
    assert mailbox.create_count == 0


def test_draft_transitioned_to_scheduled_blocks_recreation():
    mailbox = FakeMailbox()
    api, store, _ = setup(mailbox)
    created = api.create_or_recover(request())
    mailbox.snapshot = MailboxSnapshot(scheduled=(ExistingMessage(
        "m-d-1", "t-d-1", ("clinic@example.jp",), "Subject",
        clinic_uuid=UUID, outreach_id=request().operation_key),))
    result = api.create_or_recover(request())
    assert created.status == "DRAFT_CREATED"
    assert result.status == "SCHEDULED_EXISTS"
    assert store.rows[request().operation_key]["status"] == "SCHEDULED"
    assert mailbox.create_count == 1


def test_sent_state_is_recorded_in_store_and_blocks_draft_creation():
    mailbox = FakeMailbox()
    mailbox.snapshot = MailboxSnapshot(sent=(ExistingMessage(
        "m-sent", "t-sent", ("clinic@example.jp",), "Subject",
        clinic_uuid=UUID, outreach_id=request().operation_key),))
    api, store, _ = setup(mailbox)
    result = api.create_or_recover(request())
    assert result.status == "SENT_EXISTS"
    assert store.rows[request().operation_key]["status"] == "SENT"
    assert mailbox.create_count == 0


def test_same_operation_key_with_different_clinic_uuid_is_rejected():
    api, _, mailbox = setup()
    api.create_or_recover(request())
    wrong_uuid = "123e4567-e89b-42d3-a456-426614174099"
    result = api.create_or_recover(request(uuid=wrong_uuid))
    assert result.status == "HOLD"
    assert mailbox.create_count == 1


def test_non_exact_same_recipient_gmail_history_holds_new_draft():
    mailbox = FakeMailbox()
    mailbox.snapshot = MailboxSnapshot(sent=(ExistingMessage(
        "sent-old", "thread-old", ("clinic@example.jp",), "Earlier subject"),))
    api, _, _ = setup(mailbox)
    result = api.create_or_recover(request())
    assert result.status == "IDENTITY_CONFLICT"
    assert mailbox.create_count == 0


def test_same_exact_draft_key_with_wrong_uuid_is_identity_conflict():
    mailbox = FakeMailbox()
    mailbox.snapshot = MailboxSnapshot(drafts=(ExistingDraft(
        "d-old", request().draft_key, ("clinic@example.jp",), "Subject", clinic_id="999",
        clinic_uuid="123e4567-e89b-42d3-a456-426614174099", outreach_id=request().operation_key),))
    api, _, _ = setup(mailbox)
    result = api.create_or_recover(request())
    assert result.status == "IDENTITY_CONFLICT"
    assert mailbox.create_count == 0


def test_different_campaign_has_distinct_operation_key_and_history_scope():
    api, _, mailbox = setup()
    one = request(operation="operation-spring-00001", campaign="spring")
    two = request(operation="operation-autumn-0001", campaign="autumn")
    assert one.draft_key != two.draft_key
    api.create_or_recover(one)
    result = api.create_or_recover(two)
    # The coordinator gives each campaign a distinct operation identity. The live
    # production clinic-wide guard may still HOLD the second campaign by policy.
    assert result.status == "HOLD"
    assert mailbox.create_count == 1


def test_trash_and_unverified_gmail_state_are_holds():
    mailbox = FakeMailbox()
    mailbox.snapshot = MailboxSnapshot(trashed=(ExistingMessage(
        "trash-1", "thread-1", ("clinic@example.jp",), "Subject",
        clinic_uuid=UUID, outreach_id=request().operation_key),))
    api, _, _ = setup(mailbox)
    result = api.create_or_recover(request())
    assert result.status == "TRASHED_EXISTS"
    assert mailbox.create_count == 0

    mailbox.snapshot = MailboxSnapshot(verified=False)
    result = api.create_or_recover(request(operation="operation-unverified-0001"))
    assert result.status == "HOLD"
    assert mailbox.create_count == 0


def test_unconfirmed_clinic_identity_is_rejected_before_reservation():
    api, store, mailbox = setup()
    unverified = DraftApiRequest("unverified-operation-0001", UUID, 101, "campaign-a",
        "clinic@example.jp", "Subject", "Body")
    assert api.create_or_recover(unverified).status == "REJECTED"
    assert store.rows == {}
    assert mailbox.create_count == 0


def test_unconfirmed_clinic_identity_is_rejected_before_reservation():
    api, store, mailbox = setup()
    unverified = DraftApiRequest("unverified-operation-0001", UUID, 101, "campaign-a",
        "clinic@example.jp", "Subject", "Body")
    assert api.create_or_recover(unverified).status == "REJECTED"
    assert store.rows == {}
    assert mailbox.create_count == 0
