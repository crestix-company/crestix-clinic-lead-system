import pytest

from src.outreach.gas_email_lookup import (
    GasEmailLookupError, lookup_primary_emails, MISSING_NOT_ELIGIBLE, MISSING_NOT_RESEARCHED,
)


class FakeEmailResearch:
    def __init__(self, rows):
        self._rows = rows

    def primary_listing(self, *, limit=500):
        return self._rows


class FakeBundle:
    def __init__(self, rows):
        self.email_research = FakeEmailResearch(rows)


class FakeStatus:
    def __init__(self, message):
        self.message = message


def test_empty_input_returns_empty_without_connecting():
    def boom(**kwargs):
        raise AssertionError("must not connect for empty input")
    assert lookup_primary_emails([], build_repositories=boom) == {}


def test_found_missing_and_ineligible_are_all_reported():
    rows = [
        {"clinic_id": 1, "candidate_id": "c1", "email": "a@example.jp", "final_sales_eligible": True},
        {"clinic_id": 2, "candidate_id": "c2", "email": "b@example.jp", "final_sales_eligible": False},
    ]
    result = lookup_primary_emails([1, 2, 3], build_repositories=lambda **k: (FakeBundle(rows), None))
    assert result[1].found and result[1].email == "a@example.jp"
    assert not result[2].found and result[2].reason == MISSING_NOT_ELIGIBLE
    assert not result[3].found and result[3].reason == MISSING_NOT_RESEARCHED


def test_connection_failure_raises_not_silently_empty():
    with pytest.raises(GasEmailLookupError, match="boom"):
        lookup_primary_emails([1], build_repositories=lambda **k: (None, FakeStatus("boom")))


@pytest.mark.parametrize("bad", ["not-a-number", "123e4567-e89b-42d3-a456-426614174000", True])
def test_non_integer_clinic_id_is_rejected_before_any_query(bad):
    def boom(**kwargs):
        raise AssertionError("must not connect when an id fails validation")
    with pytest.raises(ValueError, match="clinic_id"):
        lookup_primary_emails([bad], build_repositories=boom)


def test_numeric_string_clinic_id_is_accepted():
    rows = [{"clinic_id": 7, "candidate_id": "c7", "email": "g@example.jp", "final_sales_eligible": True}]
    result = lookup_primary_emails(["7"], build_repositories=lambda **k: (FakeBundle(rows), None))
    assert result[7].found
