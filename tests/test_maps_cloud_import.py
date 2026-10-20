"""Issue #130: tests for src.master.maps_cloud_import -- the idempotency key
(mirrors tests for src.master.comdesk_cloud_import) and, more importantly,
preview_maps_import()'s real matching-outcome preview, exercised against the
REAL SupabaseProvenanceWriteRepository._prefetch_maps_targets /
_find_target_prefetched / classify_match_status / build_maps_update (not
reimplemented or mocked), with a fake Postgres connection/cursor standing in
for the actual database. This is what lets the preview be the real matching
logic, not a guess (see that module's docstring).

counts is asserted to use the IDENTICAL shape import_maps_results() itself
returns (TOTAL/MATCHED/WEBSITE/NO_WEBSITE/NOT_FOUND/AMBIGUOUS/EXCLUDED/
ERROR/UNLINKED/PRESERVED_WEBSITE) -- deliberately, since an earlier draft
used a narrower, differently-defined vocabulary where "MATCHED" meant
something else (an existing clinic was found) than the real result's
"MATCHED" (the collector found a Maps profile). Matching the real shape
field-for-field is what this module's docstring calls the fix for that
review finding; these tests pin that the fields mean what import_maps_
results() itself means, not a reinvented definition."""
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
    def __init__(self, fetchall_results, *, raise_on_execute=None):
        self._fetchall_results = list(fetchall_results)
        self._raise_on_execute = raise_on_execute
        self.queries = []

    def execute(self, sql, params=None):
        self.queries.append((sql, params))
        if self._raise_on_execute is not None and len(self.queries) == self._raise_on_execute[0]:
            raise self._raise_on_execute[1]

    def fetchall(self):
        return self._fetchall_results.pop(0) if self._fetchall_results else []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _FakeConnection:
    def __init__(self, fetchall_results, *, raise_on_execute=None):
        self._fetchall_results = fetchall_results
        self._raise_on_execute = raise_on_execute
        self.rolled_back = False
        self.committed = False

    def cursor(self):
        return _FakeCursor(self._fetchall_results, raise_on_execute=self._raise_on_execute)

    def rollback(self):
        self.rolled_back = True

    def commit(self):
        self.committed = True


def _provenance_repo(fetchall_results, *, raise_on_execute=None):
    conn = _FakeConnection(fetchall_results, raise_on_execute=raise_on_execute)
    return SupabaseProvenanceWriteRepository(conn), conn


def test_idempotency_key_stable_for_identical_bytes_and_changes_otherwise():
    key_a = maps_import_idempotency_key(b"same content")
    key_b = maps_import_idempotency_key(b"same content")
    key_c = maps_import_idempotency_key(b"different content")
    assert key_a == key_b
    assert key_a != key_c


def test_counts_shape_matches_import_maps_results_exactly():
    """Pins the field set itself -- the fix this module's docstring
    describes is specifically about reusing this shape, not inventing one."""
    frame = _frame([_row(maps_match_status="ERROR")])
    repo, _conn = _provenance_repo([])
    result = preview_maps_import(repo, frame)
    assert set(result["counts"]) == {
        "TOTAL", "MATCHED", "WEBSITE", "NO_WEBSITE", "NOT_FOUND",
        "AMBIGUOUS", "EXCLUDED", "ERROR", "UNLINKED", "PRESERVED_WEBSITE",
    }


def test_matched_website_via_internal_clinic_id_counts_as_matched_and_linked():
    frame = _frame([_row(internal_clinic_id="5", maps_match_status="MAPS_MATCHED_WEBSITE")])
    # internal_ids branch returns one candidate row; no clinic_ids -> no base_json lookup fires.
    repo, conn = _provenance_repo([[(5, "", "", "", "", "")]])
    result = preview_maps_import(repo, frame)
    counts = result["counts"]
    assert counts["MATCHED"] == 1
    assert counts["WEBSITE"] == 1
    assert counts["NO_WEBSITE"] == 0
    assert counts["UNLINKED"] == 0  # cid=5 was found, so this row is linked
    assert result["row_count"] == 1
    assert conn.rolled_back is True
    assert conn.committed is False


def test_no_clinic_identifiers_counts_as_unlinked_regardless_of_collector_status():
    """MATCHED/WEBSITE here reflect the COLLECTOR's own maps_match_status,
    not whether our clinic master matched this row -- the real import
    counts both independently (a row can be collector-MATCHED and still
    clinic-UNLINKED at the same time, e.g. a brand-new clinic)."""
    frame = _frame([_row(maps_match_status="MAPS_MATCHED_WEBSITE")])  # no internal id, phone, name, or address
    repo, conn = _provenance_repo([])
    result = preview_maps_import(repo, frame)
    counts = result["counts"]
    assert counts["MATCHED"] == 1
    assert counts["WEBSITE"] == 1
    assert counts["UNLINKED"] == 1
    assert conn.rolled_back is True


def test_excluded_status_counts_as_excluded_and_still_unlinked_without_identifiers():
    frame = _frame([_row(maps_match_status="EXCLUDED_hospital")])
    repo, _conn = _provenance_repo([])
    result = preview_maps_import(repo, frame)
    counts = result["counts"]
    assert counts["EXCLUDED"] == 1
    assert counts["MATCHED"] == 0
    assert counts["UNLINKED"] == 1


def test_error_status_counts_as_error_and_still_unlinked_without_identifiers():
    frame = _frame([_row(maps_match_status="ERROR")])
    repo, _conn = _provenance_repo([])
    result = preview_maps_import(repo, frame)
    counts = result["counts"]
    assert counts["ERROR"] == 1
    assert counts["UNLINKED"] == 1


def test_ambiguous_clinic_match_counts_as_unlinked_not_a_separate_bucket():
    """import_maps_results() itself has no separate counter for "multiple
    candidate clinics, could not disambiguate" -- find_target()/
    _find_target_prefetched() returning (None, "AMBIGUOUS", 0) still just
    means cid is None, which only ever increments UNLINKED. The preview
    must reproduce that exactly, not invent a bucket the real import
    doesn't have."""
    frame = _frame([_row(source_phone="03-1234-5678", maps_match_status="MAPS_MATCHED_WEBSITE")])
    key = tel_match_key("03-1234-5678")
    # phone_keys branch: two clinics share the same tel_match_key.
    phone_rows = [(11, "", "", key, "name-a", "addr-a"), (12, "", "", key, "name-b", "addr-b")]
    # duplicate_phone_ids branch: effective_json for both candidates, scored equally close.
    effective_rows = [
        (11, '{"clinic_name": "medical clinic a", "address": "tokyo a"}'),
        (12, '{"clinic_name": "medical clinic b", "address": "tokyo b"}'),
    ]
    repo, _conn = _provenance_repo([phone_rows, effective_rows])
    result = preview_maps_import(repo, frame)
    counts = result["counts"]
    assert counts["UNLINKED"] == 1
    assert counts["MATCHED"] == 1  # collector-side status is independent of clinic-match outcome
    assert counts["AMBIGUOUS"] == 0  # this bucket is collector-status-driven (MAPS_AMBIGUOUS), not clinic-match-driven


def test_collector_reported_maps_ambiguous_status_counts_in_ambiguous_bucket():
    """The AMBIGUOUS bucket IS populated, but by classify_match_status()
    reading the collector's own maps_match_status=="MAPS_AMBIGUOUS" --
    the collector itself could not tell which Maps listing was the
    business, independent of our clinic-matching step entirely."""
    frame = _frame([_row(maps_match_status="MAPS_AMBIGUOUS")])
    repo, _conn = _provenance_repo([])
    result = preview_maps_import(repo, frame)
    counts = result["counts"]
    assert counts["AMBIGUOUS"] == 1
    assert counts["MATCHED"] == 0


def test_preserved_website_counted_when_existing_confirmed_website_would_be_downgraded():
    """Mirrors import_maps_results()'s own anti-downgrade rule via the same
    build_maps_update() pure function: an existing clinic with a confirmed
    website must show as PRESERVED_WEBSITE in the preview when the incoming
    row would otherwise downgrade it (e.g. NO_WEBSITE this batch)."""
    frame = _frame([_row(internal_clinic_id="5", maps_match_status="MAPS_MATCHED_NO_WEBSITE")])
    base_json = '{"maps_presence_status": "MAPS_MATCHED_WEBSITE", "maps_website_url": "https://existing.example.jp"}'
    repo, conn = _provenance_repo([
        [(5, "", "", "", "", "")],  # internal_ids candidate lookup
        [(5, base_json)],  # base_json lookup for the matched clinic
    ])
    result = preview_maps_import(repo, frame)
    counts = result["counts"]
    assert counts["PRESERVED_WEBSITE"] == 1
    assert conn.rolled_back is True
    assert conn.committed is False


def test_preview_never_commits_only_rolls_back():
    frame = _frame([_row(maps_match_status="ERROR")])
    repo, conn = _provenance_repo([])
    preview_maps_import(repo, frame)
    assert conn.rolled_back is True
    assert conn.committed is False


def test_missing_required_columns_raises_value_error_before_touching_the_connection():
    frame = pd.DataFrame([{"only_one_column": "x"}])
    repo, conn = _provenance_repo([])
    with pytest.raises(ValueError):
        preview_maps_import(repo, frame)
    # validate_maps_frame() raises before any cursor is ever opened -- the
    # connection is never touched, so rollback is correctly never called.
    assert conn.rolled_back is False


def test_sql_error_during_prefetch_still_rolls_back_and_propagates():
    """Requirement: any exception during DB matching -- a SQL error, not
    just a ValueError -- must still reach rollback() before propagating."""
    frame = _frame([_row(internal_clinic_id="5", maps_match_status="MAPS_MATCHED_WEBSITE")])
    repo, conn = _provenance_repo([], raise_on_execute=(1, RuntimeError("simulated SQL error")))
    with pytest.raises(RuntimeError, match="simulated SQL error"):
        preview_maps_import(repo, frame)
    assert conn.rolled_back is True
    assert conn.committed is False


def test_exception_in_matching_function_still_rolls_back_and_propagates(monkeypatch):
    """Same guarantee, but for an exception raised by the matching/
    classification logic itself (not a SQL error) -- e.g. a malformed row
    the matcher cannot handle."""
    frame = _frame([_row(internal_clinic_id="5", maps_match_status="MAPS_MATCHED_WEBSITE")])
    repo, conn = _provenance_repo([[(5, "", "", "", "", "")]])

    def _boom(row, indexes):
        raise RuntimeError("simulated matcher failure")

    monkeypatch.setattr(type(repo), "_find_target_prefetched", staticmethod(_boom))
    with pytest.raises(RuntimeError, match="simulated matcher failure"):
        preview_maps_import(repo, frame)
    assert conn.rolled_back is True
    assert conn.committed is False
