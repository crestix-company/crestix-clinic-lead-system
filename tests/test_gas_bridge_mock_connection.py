"""Mock-connection integration tests (item 5/6 of the Python-side task):
auth, timeout, partial success, retry, and duplicate registration -- all
against the in-memory `MockGasBridgeServer`, never a real network call.
"""
from __future__ import annotations

import json
import time

from src.outreach.gas_bridge_client import build_auth_fields

import pytest

from src.outreach.gas_bridge_client import (
    GasBridgeClient,
    GasCaseRegistrationRequest,
    GasCaseStatus,
)
from src.outreach.gas_bridge_orchestrator import ExistingGmailDraft, register_with_existing_draft
from tests.support.mock_gas_bridge_server import MockGasBridgeServer, make_mock_transport

UUID1 = "123e4567-e89b-42d3-a456-426614174000"
UUID2 = "123e4567-e89b-42d3-a456-426614174001"
SECRET = "test-shared-secret"


def _request(**overrides):
    base = dict(
        operation_key="a" * 64,
        clinic_uuid=UUID1,
        clinic_name="ひろクリニック",
        recipient_email="info@hiroclinic.jp",
        existing_draft_id="draft-1",
    )
    base.update(overrides)
    return GasCaseRegistrationRequest(**base)


def test_successful_registration_through_mock_server_with_real_auth_signing():
    server = MockGasBridgeServer(shared_secret=SECRET, known_draft_ids={"draft-1"})
    client = GasBridgeClient(make_mock_transport(server, shared_secret=SECRET))

    result = client.register_case(_request())

    assert result.status == GasCaseStatus.REGISTERED
    assert result.draft_id == "draft-1"
    assert server.call_count == 1
    assert len(server.accepted_nonces) == 1


def _signed_wire(payload, *, secret=SECRET, timestamp=None, nonce="test_nonce_abcdefghijklmnop"):
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    auth = build_auth_fields(
        payload_json,
        secret,
        now=time.time() if timestamp is None else timestamp,
        nonce=nonce,
    )
    return json.dumps({"auth": auth, "payload": payload_json}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def test_mock_server_rejects_signature_mismatch_before_registration():
    server = MockGasBridgeServer(shared_secret=SECRET, known_draft_ids={"draft-1"})
    envelope = json.loads(_signed_wire(_request().to_json()))
    envelope["auth"]["signature"] = "0" * 64

    status, _ = server.handle_wire(json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")))

    assert status == 401
    assert server.cases_by_operation_key == {}
    assert server.accepted_nonces == set()


def test_mock_server_rejects_expired_timestamp_and_replay_before_side_effects():
    server = MockGasBridgeServer(shared_secret=SECRET, known_draft_ids={"draft-1"}, now=1_800_000_000)
    payload = _request().to_json()
    expired = _signed_wire(payload, timestamp=1_700_000_000, nonce="expired_nonce_abcdefghijkl")
    assert server.handle_wire(expired)[0] == 401
    assert server.cases_by_operation_key == {}

    server.now = time.time()
    valid = _signed_wire(payload, timestamp=server.now, nonce="replay_nonce_abcdefghijkl")
    assert server.handle_wire(valid)[0] == 200
    replay_status, _ = server.handle_wire(valid)
    assert replay_status == 401
    assert len(server.cases_by_operation_key) == 1


def test_auth_failure_wrong_shared_secret_does_not_register_anything():
    server = MockGasBridgeServer(shared_secret=SECRET, known_draft_ids={"draft-1"})
    client = GasBridgeClient(make_mock_transport(server, shared_secret="wrong-secret"))

    result = client.register_case(_request())

    assert result.status == GasCaseStatus.AUTH_ERROR
    assert not result.is_success
    assert server.cases_by_operation_key == {}


def test_timeout_maps_to_retryable_transport_error():
    server = MockGasBridgeServer(shared_secret=SECRET, known_draft_ids={"draft-1"}, always_timeout=True)
    client = GasBridgeClient(make_mock_transport(server, shared_secret=SECRET))

    result = client.register_case(_request())

    assert result.status == GasCaseStatus.TRANSPORT_ERROR
    assert result.is_retryable


def test_retry_after_timeout_uses_fresh_nonce_and_preserves_operation_key():
    server = MockGasBridgeServer(shared_secret=SECRET, known_draft_ids={"draft-1"}, always_timeout=True)
    client = GasBridgeClient(make_mock_transport(server, shared_secret=SECRET))
    request = _request()

    first = client.register_case(request)
    server.always_timeout = False
    second = client.register_case(request)

    assert first.status == GasCaseStatus.TRANSPORT_ERROR
    assert second.status == GasCaseStatus.REGISTERED
    assert len(server.accepted_nonces) == 1
    assert request.operation_key in server.cases_by_operation_key


def test_unexpected_http_status_is_treated_as_transport_error():
    server = MockGasBridgeServer(shared_secret=SECRET, known_draft_ids={"draft-1"}, always_return_http_status=500)
    client = GasBridgeClient(make_mock_transport(server, shared_secret=SECRET))

    result = client.register_case(_request())

    assert result.status == GasCaseStatus.TRANSPORT_ERROR
    assert result.is_retryable


def test_duplicate_registration_with_different_draft_id_keeps_original_draft():
    """Two calls, same operationKey, a (mistaken) different existingDraft.id on
    the second call -- the GAS-side contract says the sheet's original record
    wins. The mock server encodes that same rule."""
    server = MockGasBridgeServer(shared_secret=SECRET, known_draft_ids={"draft-1", "draft-2"})
    client = GasBridgeClient(make_mock_transport(server, shared_secret=SECRET))

    first = client.register_case(_request(existing_draft_id="draft-1"))
    second = client.register_case(_request(existing_draft_id="draft-2"))

    assert first.status == GasCaseStatus.REGISTERED
    assert second.status == GasCaseStatus.ALREADY_REGISTERED
    assert second.draft_id == "draft-1", "the sheet's original draft reference must win, not the retry's"
    assert server.call_count == 2


def test_retry_after_pending_recovers_without_duplicate_registration():
    """Simulates: draft verified, sheet write failed once (PENDING), caller
    retries the same request -- registration completes (RECOVERED) and no
    second draft is required."""
    server = MockGasBridgeServer(shared_secret=SECRET, known_draft_ids={"draft-1"}, fail_next_n_sheet_writes=1)
    client = GasBridgeClient(make_mock_transport(server, shared_secret=SECRET))
    request = _request()

    first = client.register_case(request)
    assert first.status == GasCaseStatus.PENDING
    assert first.is_retryable

    second = client.register_case(request)
    assert second.status == GasCaseStatus.RECOVERED
    assert second.draft_id == "draft-1"
    assert second.is_success
    assert len(server.accepted_nonces) == 2, "each retry must use a fresh nonce"


def test_rejected_draft_not_found_does_not_retry_automatically():
    server = MockGasBridgeServer(shared_secret=SECRET, known_draft_ids=set())
    client = GasBridgeClient(make_mock_transport(server, shared_secret=SECRET))

    result = client.register_case(_request(existing_draft_id="missing-draft"))

    assert result.status == GasCaseStatus.REJECTED
    assert result.is_terminal_failure
    assert not result.is_retryable


def test_orchestrator_ledger_prevents_registering_a_different_draft_on_retry(tmp_path):
    """Exercises item 4 (Draft ID優先) via the orchestrator: a retry that
    passes a *different* ExistingGmailDraft for the same operation_key must
    still register the originally-recorded draft_id, because the local
    ledger is consulted before building the request."""
    server = MockGasBridgeServer(shared_secret=SECRET, known_draft_ids={"draft-1", "draft-2"})
    client = GasBridgeClient(make_mock_transport(server, shared_secret=SECRET))
    ledger_path = tmp_path / "case_ledger.jsonl"
    operation_key = "b" * 64

    first = register_with_existing_draft(
        client,
        operation_key=operation_key,
        clinic_uuid=UUID1,
        clinic_name="ひろクリニック",
        recipient_email="info@hiroclinic.jp",
        existing_draft=ExistingGmailDraft(draft_id="draft-1"),
        ledger_path=ledger_path,
    )
    second = register_with_existing_draft(
        client,
        operation_key=operation_key,
        clinic_uuid=UUID1,
        clinic_name="ひろクリニック",
        recipient_email="info@hiroclinic.jp",
        existing_draft=ExistingGmailDraft(draft_id="draft-2"),  # mistaken different draft on retry
        ledger_path=ledger_path,
    )

    assert first.status == GasCaseStatus.REGISTERED
    assert first.draft_id == "draft-1"
    assert second.status == GasCaseStatus.ALREADY_REGISTERED
    assert second.draft_id == "draft-1", "ledger must have substituted the original draft_id, not draft-2"

    ledger_contents = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
    assert len(ledger_contents) == 2
    assert all(record["draft_id"] == "draft-1" for record in ledger_contents)


def test_orchestrator_records_ledger_entry_for_transport_failures_too(tmp_path):
    """Even a failed attempt (timeout) is recorded, so operators can see that
    GAS was never confirmed to have processed this operation_key."""
    server = MockGasBridgeServer(shared_secret=SECRET, known_draft_ids={"draft-1"}, always_timeout=True)
    client = GasBridgeClient(make_mock_transport(server, shared_secret=SECRET))
    ledger_path = tmp_path / "case_ledger.jsonl"

    result = register_with_existing_draft(
        client,
        operation_key="c" * 64,
        clinic_uuid=UUID1,
        clinic_name="ひろクリニック",
        recipient_email="info@hiroclinic.jp",
        existing_draft=ExistingGmailDraft(draft_id="draft-1"),
        ledger_path=ledger_path,
    )

    assert result.status == GasCaseStatus.TRANSPORT_ERROR
    ledger_contents = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
    assert ledger_contents[0]["status"] == "TRANSPORT_ERROR"
