from contextlib import contextmanager
from datetime import datetime, timezone

from src.repository.shared_draft_lease_repository import SharedDraftLeaseRepository


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def execute(self, sql, params):
        self.connection.calls.append((sql, params))
        self.row = self.connection.rows.pop(0)
    def fetchone(self): return self.row


class FakeConnection:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []
    @contextmanager
    def transaction(self):
        yield
    def cursor(self): return FakeCursor(self)


def test_acquire_passes_verified_gas_case_and_fencing_data():
    expires = datetime.now(timezone.utc)
    conn = FakeConnection([(True, "PENDING", 7, "", expires)])
    repo = SharedDraftLeaseRepository(conn)
    lease = repo.acquire(operation_key="op-1", clinic_uuid="uuid", clinic_id=101,
        campaign_id="campaign", gas_case_id="GAS-1", owner_token="owner", lease_seconds=90)
    assert lease.acquired and lease.fencing_token == 7
    assert conn.calls[0][1][4] == "GAS-1"
    assert conn.calls[0][1][5] == "owner"


def test_complete_and_reconcile_use_fenced_and_gmail_evidence_functions():
    conn = FakeConnection([(True,), (True,)])
    repo = SharedDraftLeaseRepository(conn)
    assert repo.complete(operation_key="op-1", owner_token="owner", fencing_token=3,
        draft_id="draft", message_id="message", thread_id="thread")
    assert "complete_shared_draft_operation" in conn.calls[0][0]
    assert conn.calls[0][1][2] == 3
    assert repo.reconcile_existing(operation_key="op-1", clinic_uuid="uuid", clinic_id=101,
        campaign_id="campaign", draft_id="draft", message_id="message", thread_id="thread")
    assert "observe_shared_draft_state" in conn.calls[1][0]
    assert conn.calls[1][1][5] == "op-1"
