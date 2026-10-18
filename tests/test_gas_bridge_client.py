import json

import pytest

from src.outreach.gas_bridge_client import (
    GasBridgeClient,
    GasBridgeTransportError,
    GasCaseRegistrationRequest,
    GasCaseStatus,
    ScheduleCandidate,
    build_auth_fields,
    parse_response,
    validate_request,
)

UUID1 = "123e4567-e89b-42d3-a456-426614174000"
VALID_OPERATION_KEY = "a" * 64


def _valid_request(**overrides):
    base = dict(
        operation_key=VALID_OPERATION_KEY,
        clinic_uuid=UUID1,
        clinic_name="ひろクリニック",
        recipient_email="info@hiroclinic.jp",
        existing_draft_id="draft-1",
    )
    base.update(overrides)
    return GasCaseRegistrationRequest(**base)


def test_to_json_matches_documented_contract_shape():
    request = _valid_request(clinic_id="33147", director_name="山田 太郎", is_name="高原", fs_name="松岡")
    payload = request.to_json()

    assert payload == {
        "operationKey": VALID_OPERATION_KEY,
        "clinicUuid": UUID1,
        "clinicId": "33147",
        "clinicName": "ひろクリニック",
        "directorName": "山田 太郎",
        "recipientEmail": "info@hiroclinic.jp",
        "isName": "高原",
        "fsName": "松岡",
        "existingDraft": {"id": "draft-1"},
    }


def test_to_json_includes_schedule_when_present():
    request = _valid_request(schedule=(ScheduleCandidate(date="2026/10/20", start="13:00", end="15:00", display="10月20日（火）13:00～15:00", available_fs="松岡・魚井"),))
    payload = request.to_json()
    assert payload["schedule"] == [
        {"date": "2026/10/20", "start": "13:00", "end": "15:00", "display": "10月20日（火）13:00～15:00", "availableFs": "松岡・魚井"}
    ]


@pytest.mark.parametrize(
    "overrides,expected_substring",
    [
        ({"operation_key": "short"}, "operation_key"),
        ({"clinic_uuid": "not-a-uuid"}, "clinic_uuid"),
        ({"recipient_email": "not-an-email"}, "recipient_email"),
        ({"clinic_name": ""}, "clinic_name"),
        ({"existing_draft_id": ""}, "existing_draft_id"),
    ],
)
def test_validate_request_rejects_malformed_fields(overrides, expected_substring):
    request = _valid_request(**overrides)
    errors = validate_request(request)
    assert any(expected_substring in error for error in errors)


def test_validate_request_accepts_well_formed_request():
    assert validate_request(_valid_request()) == []


def test_register_case_rejects_locally_without_calling_transport():
    calls = []

    def transport(payload, *, timeout_seconds):
        calls.append(payload)
        return 200, json.dumps({"status": "REGISTERED"})

    client = GasBridgeClient(transport)
    result = client.register_case(_valid_request(clinic_uuid="not-a-uuid"))

    assert result.status == GasCaseStatus.REJECTED
    assert "clinic_uuid" in result.reason
    assert calls == [], "invalid requests must never reach the transport"


def test_register_case_parses_successful_response():
    def transport(payload, *, timeout_seconds):
        return 200, json.dumps(
            {"status": "REGISTERED", "caseId": "20261018-090000-001", "draftId": "draft-1", "threadId": "thread-1", "trackingToken": "t" * 32}
        )

    client = GasBridgeClient(transport)
    result = client.register_case(_valid_request())

    assert result.status == GasCaseStatus.REGISTERED
    assert result.is_success
    assert result.case_id == "20261018-090000-001"
    assert result.thread_id == "thread-1"


def test_register_case_maps_401_to_auth_error_without_parsing_body():
    def transport(payload, *, timeout_seconds):
        return 401, "unauthorized"

    client = GasBridgeClient(transport)
    result = client.register_case(_valid_request())

    assert result.status == GasCaseStatus.AUTH_ERROR
    assert not result.is_success
    assert not result.is_retryable


def test_register_case_maps_transport_exception_to_transport_error():
    def transport(payload, *, timeout_seconds):
        raise GasBridgeTransportError("connection timed out")

    client = GasBridgeClient(transport)
    result = client.register_case(_valid_request())

    assert result.status == GasCaseStatus.TRANSPORT_ERROR
    assert result.is_retryable
    assert result.reason == "transport failed"


def test_register_case_handles_malformed_json_response():
    def transport(payload, *, timeout_seconds):
        return 200, "not json"

    client = GasBridgeClient(transport)
    result = client.register_case(_valid_request())

    assert result.status == GasCaseStatus.INVALID_RESPONSE


def test_register_case_handles_unrecognized_status_value():
    def transport(payload, *, timeout_seconds):
        return 200, json.dumps({"status": "SOMETHING_NEW"})

    client = GasBridgeClient(transport)
    result = client.register_case(_valid_request())

    assert result.status == GasCaseStatus.INVALID_RESPONSE


def test_pending_status_is_retryable_but_not_success():
    result = parse_response({"status": "PENDING", "reason": "in progress"})
    assert result.is_retryable
    assert not result.is_success


def test_already_registered_is_success_not_retryable():
    result = parse_response({"status": "ALREADY_REGISTERED", "caseId": "x"})
    assert result.is_success
    assert not result.is_retryable


def test_build_auth_fields_is_deterministic_for_same_timestamp_and_changes_with_body():
    first = build_auth_fields('{"a":1}', "secret", now=1700000000, nonce="abcdefghijklmnopqrstuvwx")
    second = build_auth_fields('{"a":1}', "secret", now=1700000000, nonce="abcdefghijklmnopqrstuvwx")
    different_body = build_auth_fields('{"a":2}', "secret", now=1700000000, nonce="abcdefghijklmnopqrstuvwx")

    assert first == second
    assert first["signature"] != different_body["signature"]
    assert "sharedSecret" not in first
    assert first["nonce"] == "abcdefghijklmnopqrstuvwx"


def test_build_auth_fields_requires_shared_secret():
    with pytest.raises(ValueError):
        build_auth_fields("{}", "")


def test_build_auth_fields_rejects_non_ten_digit_timestamp_and_short_nonce():
    with pytest.raises(ValueError):
        build_auth_fields("{}", "secret", now=1000)
    with pytest.raises(ValueError):
        build_auth_fields("{}", "secret", now=1700000000, nonce="short")


def test_build_auth_fields_matches_shared_gas_golden_vector():
    payload = '{"clinicName":"架空医院","clinicUuid":"123e4567-e89b-42d3-a456-426614174000","operationKey":"mock-operation-key"}'
    auth = build_auth_fields(
        payload,
        "mock-only-shared-secret-never-production",
        now=1792310400,
        nonce="abcdefghijklmnopqrstuvwx",
    )

    assert auth == {
        "timestamp": "1792310400",
        "nonce": "abcdefghijklmnopqrstuvwx",
        "signature": "65b760feb1bf5336842237fee8adcd2ae039314f32ed526f3e48d6855f8c4fe2",
    }
