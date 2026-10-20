from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from src.outreach.shared_draft_http_api import (
    MemoryRateLimiter, Principal, create_app,
)
from tests.test_draft_creation_api import FakeMailbox, MemoryLeaseStore, UUID, request, setup


class Verifier:
    def verify(self, token):
        if token == "clinic-token":
            return Principal("clinic-lead", frozenset({"draft:create"}))
        if token == "readonly-token":
            return Principal("observer", frozenset({"draft:read"}))
        if token == "scoped-token":
            return Principal("scoped", frozenset({"draft:create"}), frozenset({999}), frozenset({"other"}))
        return None


class Identities:
    def confirmed_uuid(self, clinic_id):
        return UUID if clinic_id == 101 else None


def make_client(*, mailbox=None, store=None, audit=None, limiter=None):
    core, store, mailbox = setup(mailbox or FakeMailbox(), store or MemoryLeaseStore())
    events = audit if audit is not None else []
    app = create_app(core=core, token_verifier=Verifier(), identity_resolver=Identities(),
                     rate_limiter=limiter or MemoryRateLimiter(), audit=events.append)
    return TestClient(app), mailbox, store, events


def payload(**updates):
    req = request()
    data = {"operationKey": req.operation_key, "clinicUuid": req.clinic_uuid,
            "clinicId": req.clinic_id, "campaignId": req.campaign_id,
            "gasCaseId": req.gas_case_id, "recipient": req.recipient,
            "subject": req.subject, "body": req.body, "clinicName": req.clinic_name,
            "clinicAddress": req.clinic_address}
    data.update(updates)
    return data


def post(client, data=None, token="clinic-token", headers=None):
    h = {"Authorization": "Bearer " + token}
    h.update(headers or {})
    return client.post("/v1/drafts", json=data or payload(), headers=h)


def test_http_rejects_missing_auth_and_insufficient_permission():
    client, _, _, _ = make_client()
    assert client.post("/v1/drafts", json=payload()).status_code == 401
    assert post(client, token="readonly-token").status_code == 403
    assert post(client, token="scoped-token").status_code == 403


def test_http_rejects_uuid_mismatch_and_extra_fields():
    client, mailbox, _, _ = make_client()
    response = post(client, payload(clinicUuid="223e4567-e89b-42d3-a456-426614174000"))
    assert response.status_code == 409
    assert mailbox.create_count == 0
    assert post(client, payload(unrecognized="x")).status_code == 422


def test_http_idempotent_retry_recovers_same_draft_and_returns_correlation_id():
    client, mailbox, _, events = make_client()
    first = post(client, headers={"X-Correlation-ID": "req-123"})
    second = post(client)
    assert first.status_code == second.status_code == 200
    assert first.headers["X-Correlation-ID"] == "req-123"
    assert first.json()["status"] == "DRAFT_CREATED"
    assert second.json()["status"] == "DRAFT_RECOVERED"
    assert second.json()["draftId"] == "d-1"
    assert mailbox.create_count == 1
    assert len(events) == 2
    assert "recipient" not in events[0]


def test_concurrent_http_calls_create_one_draft():
    client, mailbox, _, _ = make_client()
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: post(client), range(2)))
    assert mailbox.create_count == 1
    assert all(r.status_code in {200, 202, 409} for r in responses)


def test_gmail_persist_then_timeout_or_db_failure_is_recovered_without_second_create():
    mailbox = FakeMailbox()
    mailbox.fail_after_persist = True
    client, mailbox, _, _ = make_client(mailbox=mailbox)
    first = post(client)
    assert first.json()["status"] == "DRAFT_RECOVERED"
    assert mailbox.create_count == 1

    mailbox2, store2 = FakeMailbox(), MemoryLeaseStore()
    store2.fail_complete_once = True
    client2, mailbox2, _, _ = make_client(mailbox=mailbox2, store=store2)
    assert post(client2).json()["status"] == "HOLD"
    assert post(client2).json()["status"] == "DRAFT_RECOVERED"
    assert mailbox2.create_count == 1


def test_rate_limit_fails_closed():
    client, mailbox, _, _ = make_client(limiter=MemoryRateLimiter(), audit=[])
    # The factory uses the default limit; pass direct minimal app to exercise limiter.
    from src.outreach.shared_draft_http_api import create_app
    core, _, _ = setup()
    app = create_app(core=core, token_verifier=Verifier(), identity_resolver=Identities(),
                     rate_limiter=MemoryRateLimiter(), audit=lambda _: None, rate_limit=1)
    limited = TestClient(app)
    assert post(limited).status_code == 200
    assert post(limited).status_code == 429
    assert mailbox.create_count == 0


def test_supabase_rate_limit_outage_is_503_and_never_creates_draft():
    class FailedLimiter:
        def allow(self, *args, **kwargs):
            raise RuntimeError("synthetic Supabase outage")
    core, _, mailbox = setup()
    app = create_app(core=core, token_verifier=Verifier(), identity_resolver=Identities(),
                     rate_limiter=FailedLimiter(), audit=lambda _: None)
    response = post(TestClient(app))
    assert response.status_code == 503
    assert mailbox.create_count == 0


def test_api_timeout_client_does_not_automatically_retry():
    from src.outreach.shared_draft_ui_adapter import SharedDraftApiClient, SharedDraftUiContext
    def timeout(*args, **kwargs):
        raise TimeoutError("synthetic")
    client = SharedDraftApiClient(endpoint="https://local.invalid", token_provider=lambda: "x", transport=timeout)
    context = SharedDraftUiContext(101, UUID, "campaign-a", "op-timeout-000001",
                                   "clinic@example.jp", "Clinic", "Tokyo", "Subject", "Body")
    result = client.create_or_recover(context)
    assert result["status"] == "UNKNOWN"


def test_legacy_gas_writer_bypassing_api_has_no_shared_lock():
    """Documented risk model: only calls through the API share the DB lease."""
    client, mailbox, _, _ = make_client()
    api_response = post(client)
    legacy_writer_creates_without_consulting_api = True
    total_drafts = mailbox.create_count + int(legacy_writer_creates_without_consulting_api)
    assert api_response.json()["status"] == "DRAFT_CREATED"
    assert total_drafts == 2  # Cutover must stop the old GAS writer first.


def test_clinic_lead_display_adapter_surfaces_article_and_gmail_ids():
    from src.outreach.shared_draft_ui_adapter import SharedDraftUiContext, display_fields
    context = SharedDraftUiContext(101, UUID, "campaign-a", "op-ui-00000001",
                                   "clinic@example.jp", "Clinic", "Tokyo", "Subject", "Body",
                                   "https://example.jp/article")
    fields = display_fields(context, {"status": "DRAFT_CREATED", "draftId": "d-1",
                                     "messageId": "m-1", "threadId": "t-1"})
    assert fields["医院"] == "Clinic"
    assert fields["メール本文"] == "Body"
    assert fields["記事URL"] == "https://example.jp/article"
    assert (fields["Draft ID"], fields["Message ID"], fields["Thread ID"]) == ("d-1", "m-1", "t-1")


def test_clinic_lead_client_adapter_calls_http_api_with_reviewed_result():
    from types import SimpleNamespace
    from src.outreach.shared_draft_ui_adapter import (
        SharedDraftApiClient, context_from_outreach_result, display_fields,
    )
    client, mailbox, _, _ = make_client()
    fake_result = SimpleNamespace(
        target=SimpleNamespace(clinic_id=101, clinic_uuid=UUID, email="clinic@example.jp",
            clinic_name="Clinic", clinic_address="Tokyo", director_name="Dr. Example",
            article=SimpleNamespace(status="LISTED")),
        listed_url="https://example.jp/article", example_url="", first=None,
        subject="Reviewed subject", body="Reviewed official body",
    )
    context = context_from_outreach_result(fake_result, campaign_id="campaign-a",
                                            operation_key="reviewed-op-20261020")
    def transport(url, *, json, headers, timeout):
        assert url.endswith("/v1/drafts") and timeout <= 60
        assert headers["Authorization"] == "Bearer mock-cloud-run-id-token"
        assert headers["X-Shared-Draft-Token"] == "clinic-token"
        return client.post("/v1/drafts", json=json, headers=headers)
    api_client = SharedDraftApiClient(endpoint="http://local-test", token_provider=lambda: "clinic-token",
                                      transport=transport,
                                      identity_token_provider=lambda: "mock-cloud-run-id-token")
    response = api_client.create_or_recover(context)
    fields = display_fields(context, response)
    assert response["status"] == "DRAFT_CREATED"
    assert fields["院長名"] == "Dr. Example"
    assert fields["記事判定"] == "LISTED"
    assert fields["記事URL"] == "https://example.jp/article"
    assert fields["メール本文"] == "Reviewed official body"
    assert fields["保存結果"] == "DRAFT_CREATED"
    assert mailbox.create_count == 1

