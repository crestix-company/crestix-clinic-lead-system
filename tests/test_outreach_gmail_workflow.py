from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from email import message_from_bytes, policy
from threading import Lock

from src.outreach.compose import ARTICLE_NOT_LISTED, ARTICLE_LISTED
from src.outreach.generation import READY, OutreachResult
from src.outreach.gmail_drafts import ExistingDraft
from src.outreach.gmail_oauth import GmailConnection, REQUIRED_SCOPES
from src.outreach.gmail_workflow import (
    DraftWorkflowResult, GmailState, classify_target_state, create_gmail_draft,
)
from src.outreach.targets import EMAIL_FOUND, ArticleState, ClinicTarget
from src.repository.outreach_delivery_repository import DraftReservation


class Request:
    def __init__(self, value): self.value = value
    def execute(self): return self.value


class GmailService:
    def __init__(self, *, sent=(), scheduled=(), fail_after_create=False, fail_history=False):
        self.stored_drafts = {}
        self.sent = list(sent)
        self.scheduled = list(scheduled)
        self.fail_after_create = fail_after_create
        self.fail_history = fail_history
        self.create_calls = 0
        self._lock = Lock()

    def users(self): return self
    def messages(self): return self
    def drafts(self): return self
    def list(self, *, q=None, **kwargs):
        if self.fail_history:
            raise TimeoutError("transport failure")
        if q == "in:scheduled":
            rows = self.scheduled
        elif q:
            rows = self.sent
        else:
            rows = [{"id": key} for key in self.stored_drafts]
        return Request({"drafts" if q is None else "messages": rows})
    def get(self, *, userId, id, format=None, metadataHeaders=None):
        if id in self.stored_drafts:
            return Request({"id": id, "message": {"id": "msg-" + id, "threadId": "thread-" + id,
                                                     "payload": {"headers": self.stored_drafts[id]}}})
        for row in (*self.sent, *self.scheduled):
            if row.get("id") == id:
                return Request(row)
        raise KeyError(id)
    def create(self, *, userId, body):
        raw = body["message"]["raw"]
        raw += "=" * (-len(raw) % 4)
        msg = message_from_bytes(base64.urlsafe_b64decode(raw), policy=policy.default)
        with self._lock:
            self.create_calls += 1
            draft_id = f"d{self.create_calls}"
            headers = [{"name": name, "value": str(value)} for name, value in msg.items()]
            self.stored_drafts[draft_id] = headers
        if self.fail_after_create:
            self.fail_after_create = False
            raise TimeoutError("ambiguous response")
        return Request({"id": draft_id, "message": {"id": "m-" + draft_id, "threadId": "t-" + draft_id}})


class FakeRepository:
    def __init__(self, *, allow_reserve=True, fail_update=False):
        self.allow_reserve = allow_reserve
        self.fail_update = fail_update
        self.rows = []
        self.results = []
        self._lock = Lock()
    def health_check(self): return True
    def campaign_rows(self, campaign_id): return list(self.rows)
    def reserve_draft(self, **fields):
        with self._lock:
            existing = next((r for r in self.rows if r["clinic_id"] == fields["clinic_id"]
                             and r["campaign_id"] == fields["campaign_id"]), None)
            if existing:
                return DraftReservation(existing["outreach_id"], existing["operation_id"],
                                        existing["idempotency_key"], False, existing["status"],
                                        existing.get("gmail_draft_id", ""))
            reservation = DraftReservation("out-1", fields["operation_id"], "key-1",
                                           self.allow_reserve, "REVIEW")
            if self.allow_reserve:
                self.rows.append({"clinic_id": fields["clinic_id"],
                                  "campaign_id": fields["campaign_id"],
                                  "outreach_id": reservation.outreach_id,
                                  "operation_id": reservation.operation_id,
                                  "idempotency_key": reservation.idempotency_key,
                                  "status": "REVIEW", "gmail_draft_id": ""})
            return reservation
    def record_draft_result(self, **fields):
        if self.fail_update: raise ConnectionError("lost after Gmail draft save")
        self.results.append(fields)
        for row in self.rows:
            if row["outreach_id"] == fields["outreach_id"]:
                row["status"] = fields["status"]
                row["gmail_draft_id"] = fields.get("gmail_draft_id", "")
        return True
    def sync_status(self, **fields):
        self.results.append(fields)
        return True


def make_result(*, article_status=ARTICLE_NOT_LISTED, article_type="UNKNOWN",
                clinic_id=101, email="clinic@example.jp", director=True):
    target = ClinicTarget(
        clinic_id=clinic_id, clinic_uuid=f"uuid-{clinic_id}", clinic_name=f"Clinic {clinic_id}",
        prefecture="東京都", municipality="中央区", departments=("歯科",), treatments=(), hp_rank="A",
        director_name="院長 太郎" if director else "", email=email, email_status=EMAIL_FOUND,
        director_name_confirmed=director, clinic_address="東京都中央区1-2-3",
        article=ArticleState(article_status, article_type=article_type),
    )
    return OutreachResult(target=target, status=READY, subject="Clinic Lead subject", body="Preview body")


def connection(service):
    return GmailConnection(service, "info@crestix-inc.com", REQUIRED_SCOPES)


def create(result=None, *, service=None, repository=None, enabled=True, gmail=None):
    service = service or GmailService()
    repository = repository or FakeRepository()
    result = result or make_result()
    outcome = create_gmail_draft(
        result, campaign_id="campaign-1", generation_batch_id="0123456789abcdef0123456789abcdef",
        staff_name="Test Staff", gmail=gmail or connection(service), repository=repository,
        enabled=enabled,
    )
    return outcome, service, repository


def test_new_clinic_creates_one_draft_and_records_ids_without_send_api():
    outcome, service, repo = create()
    assert outcome.status == "DRAFT_CREATED"
    assert service.create_calls == 1
    assert repo.results[0]["status"] == "DRAFT"
    assert repo.results[0]["gmail_draft_id"] == outcome.gmail_draft_id
    assert not hasattr(service, "send")


def test_existing_draft_for_same_recipient_blocks_new_draft():
    service = GmailService()
    service.stored_drafts["prior"] = [
        {"name": "To", "value": "clinic@example.jp"},
        {"name": "Subject", "value": "old"},
    ]
    outcome, service, repo = create(service=service)
    assert outcome.status == "DRAFT_EXISTS"
    assert service.create_calls == 0
    assert not repo.rows


def test_sent_today_blocks_new_draft_but_previous_day_is_not_in_today_query():
    service = GmailService(sent=[{"id": "sent1", "threadId": "t1", "payload": {"headers": [
        {"name": "To", "value": "clinic@example.jp"}, {"name": "Subject", "value": "previous"},
    ]}}])
    outcome, service, _ = create(service=service)
    assert outcome.status == "SENT_TODAY"
    assert service.create_calls == 0


def test_scheduled_message_blocks_new_draft():
    service = GmailService(scheduled=[{"id": "sched1", "threadId": "t1", "payload": {"headers": [
        {"name": "To", "value": "clinic@example.jp"},
    ]}}])
    outcome, service, _ = create(service=service)
    assert outcome.status == "SCHEDULED_EXISTS"
    assert service.create_calls == 0


def test_pr_and_supervised_listings_are_excluded():
    for kind in ("PR", "SUPERVISED", "UNKNOWN"):
        outcome, service, _ = create(make_result(article_status=ARTICLE_LISTED, article_type=kind))
        assert outcome.status == "REVIEW"
        assert service.create_calls == 0


def test_missing_clinic_uuid_or_unverified_director_is_review():
    result = make_result(director=False)
    outcome, service, _ = create(result)
    assert outcome.status == "REVIEW"
    assert service.create_calls == 0


def test_shared_recipient_and_duplicate_clinic_rows_are_withheld_by_target_selection():
    from src.outreach.history import email_collisions
    assert email_collisions([(101, "same@example.jp"), (102, "SAME@example.jp")]) == {101, 102}


def test_gmail_history_unavailable_fails_closed_without_reservation():
    outcome, service, repo = create(service=GmailService(fail_history=True))
    assert outcome.status == "REVIEW"
    assert not repo.rows
    assert service.create_calls == 0


def test_missing_oauth_confirmation_flag_disables_draft_creation():
    outcome, service, repo = create(enabled=False)
    assert outcome.status == "DISABLED"
    assert service.create_calls == 0
    assert not repo.rows


def test_gmail_account_must_be_the_fixed_approved_sender():
    service = GmailService()
    bad = GmailConnection(service, "other@example.jp", REQUIRED_SCOPES)
    outcome, _, repo = create(gmail=bad)
    assert outcome.status == "REVIEW"
    assert service.create_calls == 0
    assert not repo.rows


def test_ledger_unique_conflict_stops_second_create():
    repo = FakeRepository(allow_reserve=False)
    outcome, service, _ = create(repository=repo)
    assert outcome.status == "REVIEW"
    assert service.create_calls == 0


def test_ambiguous_gmail_create_reconciles_exact_draft_key_once():
    service = GmailService(fail_after_create=True)
    outcome, service, repo = create(service=service)
    assert outcome.status == "DRAFT_RECOVERED"
    assert service.create_calls == 1
    assert repo.results[-1]["status"] == "DRAFT"


def test_ambiguous_create_without_exact_match_becomes_unknown_and_is_not_retried():
    service = GmailService(fail_history=False)
    repo = FakeRepository()
    # Simulate create transport failure before the Gmail side persists a draft.
    original_create = service.create
    def timeout_before_create(**kwargs):
        service.create_calls += 1
        raise TimeoutError("unknown outcome")
    service.create = timeout_before_create
    first, _, _ = create(service=service, repository=repo)
    assert first.status == "UNKNOWN"
    assert service.create_calls == 1
    second, _, _ = create(service=service, repository=repo)
    assert second.status == "UNKNOWN"
    assert service.create_calls == 1


def test_db_update_failure_after_gmail_create_returns_unknown_without_second_create():
    service = GmailService()
    repo = FakeRepository(fail_update=True)
    outcome, _, _ = create(service=service, repository=repo)
    assert outcome.status == "UNKNOWN"
    assert service.create_calls == 1


def test_concurrent_saves_reserve_once_and_create_at_most_one_draft():
    service = GmailService()
    repo = FakeRepository()
    result = make_result()
    def run():
        return create(result, service=service, repository=repo)[0]
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _x: run(), range(2)))
    assert service.create_calls == 1
    assert sum(outcome.status == "DRAFT_CREATED" for outcome in outcomes) == 1


def test_repeat_same_campaign_is_blocked_by_existing_ledger():
    service = GmailService()
    repo = FakeRepository()
    first = create(service=service, repository=repo)[0]
    second = create(service=service, repository=repo)[0]
    assert first.status == "DRAFT_CREATED"
    assert second.status in {"DRAFT_RECOVERED", "EXISTING_LEDGER"}
    assert service.create_calls == 1


def test_identity_match_prefers_ids_then_email():
    result = make_result()
    state = GmailState(drafts=(ExistingDraft("d", "", ("clinic@example.jp",), "subject"),),
                       sent_today=(), scheduled=())
    assert classify_target_state(result.target, state=state) == "DRAFT_EXISTS"
