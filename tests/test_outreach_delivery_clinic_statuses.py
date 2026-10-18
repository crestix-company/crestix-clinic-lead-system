from __future__ import annotations

from src.repository.outreach_delivery_repository import OutreachDeliveryRepository


class FakeCursor:
    def __init__(self, rows, row_factory=None):
        self._rows = rows
        self.row_factory = row_factory
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self.executed.append((sql, params))

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    def __init__(self, rows):
        self._rows = rows
        self.committed = False
        self.rolled_back = False

    def cursor(self, row_factory=None):
        return FakeCursor(self._rows, row_factory=row_factory)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


def _row(clinic_id, campaign_id, status):
    return {"clinic_id": clinic_id, "campaign_id": campaign_id, "status": status,
            "gmail_draft_id": "", "gmail_message_id": "", "sent_at": None, "created_at": None}


def test_empty_clinic_ids_returns_empty_without_querying():
    repo = OutreachDeliveryRepository(FakeConn([]))
    assert repo.clinic_statuses([]) == {}


def test_requested_clinics_with_no_records_still_appear_as_empty_list():
    rows = [_row(1, "camp-a", "DRAFT")]
    repo = OutreachDeliveryRepository(FakeConn(rows))
    result = repo.clinic_statuses([1, 2])
    assert result[1][0]["status"] == "DRAFT"
    assert result[2] == []


def test_multiple_campaigns_for_same_clinic_are_all_returned():
    rows = [_row(5, "camp-a", "SENT"), _row(5, "camp-b", "DRAFT")]
    repo = OutreachDeliveryRepository(FakeConn(rows))
    result = repo.clinic_statuses([5])
    assert {r["status"] for r in result[5]} == {"SENT", "DRAFT"}
