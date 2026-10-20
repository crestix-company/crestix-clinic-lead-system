"""Issue #130: tests for src.master.maps_cloud_import -- the idempotency key
(mirrors tests for src.master.comdesk_cloud_import) and, more importantly,
preview_maps_import()'s real matching-outcome preview, exercised against the
REAL SupabaseProvenanceWriteRepository._prefetch_maps_targets /
_find_target_prefetched (not reimplemented or mocked), with a fake Postgres
connection/cursor standing in for the actual database. This is what lets the
preview be the real matching logic, not a guess (see that module's docstring)."""
import pandas as pd
import pytest

from src.master.google_maps import MAPS_RESULT_HEADERS
from src.master.maps_cloud_import import maps_import_idempotency_key, preview_maps_import
from src.normalizer.phone import tel_match_key
from src.repository.supabase_write_adapter import SupabaseProvenanceWriteRepository


def _row(**overrides):
    row = {header: "" for header in MAPS_RESULT_HEADERS}
    row.update(overrides)
    return row


def _frame(rows):
    return pd.DataFrame(rows, columns=MAPS_RESULT_HEADERS)


class _FakeCursor:
    def __init__(self, fetchall_results):
        self._fetchall_results = list(fetchall_results)
        self.queries = []

    def execute(self, sql, params=None):
        self.queries.append((sql, params))

    def fetchall(self):
        return self._fetchall_results.pop(0) if self._fetchall_results else []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _FakeConnection:
    def __init__(self, fetchall_results):
        self._fetchall_results = fetchall_results
        self.rolled_back = False
        self.committed = False

    def cursor(self):
        return _FakeCursor(self._fetchall_results)

    def rollback(self):
        self.rolled_back = True

    def commit(self):
        self.committed = True


def _provenance_repo(fetchall_results):
    conn = _FakeConnection(fetchall_results)
    return SupabaseProvenanceWriteRepository(conn), conn


def test_idempotency_key_stable_for_identical_bytes_and_changes_otherwise():
    key_a = maps_import_idempotency_key(b"same content")
    key_b = maps_import_idempotency_key(b"same content")
    key_c = maps_import_idempotency_key(b"different content")
    assert key_a == key_b
    assert key_a != key_c


def test_matched_via_internal_clinic_id_counts_as_matched_not_new():
    frame = _frame([_row(internal_clinic_id="5", maps_match_status="MAPS_MATCHED_WEBSITE")])
    # Only the "internal_ids or med_values" branch fires; it returns one candidate row.
    repo, conn = _provenance_repo([[(5, "", "", "", "", "")]])
    result = preview_maps_import(repo, frame)
    assert result["counts"]["MATCHED"] == 1
    assert result["counts"]["NEW"] == 0
    assert result["row_count"] == 1
    assert conn.rolled_back is True
    assert conn.committed is False


def test_no_identifiers_at_all_counts_as_new_without_any_db_round_trip():
    frame = _frame([_row(maps_match_status="MAPS_MATCHED_WEBSITE")])  # no internal id, phone, name, or address
    repo, conn = _provenance_repo([])
    result = preview_maps_import(repo, frame)
    assert result["counts"]["NEW"] == 1
    assert result["counts"]["MATCHED"] == 0
    assert conn.rolled_back is True


def test_excluded_status_counted_as_excluded_even_without_any_identifiers():
    frame = _frame([_row(maps_match_status="EXCLUDED_hospital")])
    repo, conn = _provenance_repo([])
    result = preview_maps_import(repo, frame)
    assert result["counts"]["EXCLUDED"] == 1
    assert result["counts"]["NEW"] == 0


def test_error_status_counted_as_error_even_without_any_identifiers():
    frame = _frame([_row(maps_match_status="ERROR")])
    repo, conn = _provenance_repo([])
    result = preview_maps_import(repo, frame)
    assert result["counts"]["ERROR"] == 1
    assert result["counts"]["NEW"] == 0


def test_ambiguous_phone_match_with_close_scores_counts_as_ambiguous_not_matched():
    frame = _frame([_row(source_phone="03-1234-5678", maps_match_status="MAPS_MATCHED_WEBSITE")])
    key = tel_match_key("03-1234-5678")
    # phone_keys branch: two clinics share the same tel_match_key.
    phone_rows = [(11, "", "", key, "name-a", "addr-a"), (12, "", "", key, "name-b", "addr-b")]
    # duplicate_phone_ids branch: effective_json for both candidates, scored equally close.
    effective_rows = [
        (11, '{"clinic_name": "medical clinic a", "address": "tokyo a"}'),
        (12, '{"clinic_name": "medical clinic b", "address": "tokyo b"}'),
    ]
    repo, conn = _provenance_repo([phone_rows, effective_rows])
    result = preview_maps_import(repo, frame)
    assert result["counts"]["AMBIGUOUS"] == 1
    assert result["counts"]["MATCHED"] == 0
    assert result["counts"]["NEW"] == 0


def test_preview_never_commits_only_rolls_back():
    frame = _frame([_row(maps_match_status="ERROR")])
    repo, conn = _provenance_repo([])
    preview_maps_import(repo, frame)
    assert conn.rolled_back is True
    assert conn.committed is False


def test_missing_required_columns_raises_value_error():
    frame = pd.DataFrame([{"only_one_column": "x"}])
    repo, _conn = _provenance_repo([])
    with pytest.raises(ValueError):
        preview_maps_import(repo, frame)
