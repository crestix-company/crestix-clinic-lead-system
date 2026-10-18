import stat

import pytest

from src.outreach.gas_bridge_client import GasCaseRegistrationResult, GasCaseStatus
from src.outreach.gas_bridge_ledger import (
    GasBridgeLedgerError,
    find_by_operation_key,
    record_result,
)

UUID1 = "123e4567-e89b-42d3-a456-426614174000"


def test_record_result_creates_private_file_and_directory(tmp_path):
    path = tmp_path / "secrets" / "ledger.jsonl"
    result = GasCaseRegistrationResult(status=GasCaseStatus.REGISTERED, case_id="c1", draft_id="d1", thread_id="t1", tracking_token="x" * 32)

    record_result(path, operation_key="a" * 64, clinic_uuid=UUID1, result=result)

    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_find_by_operation_key_returns_most_recent_record(tmp_path):
    path = tmp_path / "ledger.jsonl"
    key = "a" * 64

    record_result(path, operation_key=key, clinic_uuid=UUID1, result=GasCaseRegistrationResult(status=GasCaseStatus.PENDING, reason="retry later"))
    record_result(path, operation_key=key, clinic_uuid=UUID1, result=GasCaseRegistrationResult(status=GasCaseStatus.REGISTERED, case_id="c1", draft_id="d1"))

    found = find_by_operation_key(path, key)
    assert found["status"] == "REGISTERED"
    assert found["draft_id"] == "d1"


def test_find_by_operation_key_returns_none_when_missing(tmp_path):
    path = tmp_path / "ledger.jsonl"
    assert find_by_operation_key(path, "missing") is None


def test_find_by_operation_key_rejects_world_readable_file(tmp_path):
    path = tmp_path / "ledger.jsonl"
    path.write_text('{"operation_key": "a", "status": "REGISTERED"}\n', encoding="utf-8")
    path.chmod(0o644)  # unsafe: group/world readable

    with pytest.raises(GasBridgeLedgerError):
        find_by_operation_key(path, "a")


def test_record_result_rejects_malformed_existing_file(tmp_path):
    path = tmp_path / "ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not valid json\n", encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(GasBridgeLedgerError):
        record_result(path, operation_key="a" * 64, clinic_uuid=UUID1, result=GasCaseRegistrationResult(status=GasCaseStatus.REGISTERED))
