from __future__ import annotations

import csv
from io import StringIO

import pytest

from src.outreach.legacy_import_preview import (
    LegacyImportInputError, compare_clinic_snapshot, parse_legacy_import_csv,
    preview_legacy_import,
)


def _csv(rows):
    columns = ["clinic_id", "clinic_uuid", "clinic_name", "clinic_address", "listing_status",
               "listing_article_url", "clinic_profile_url", "gmail_state", "gmail_draft_id",
               "gmail_message_id", "gmail_thread_id", "article_type"]
    out = StringIO()
    writer = csv.DictWriter(out, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def _row(clinic_id, listing, article_type="UNKNOWN", gmail="UNKNOWN"):
    return {"clinic_id": clinic_id, "clinic_uuid": f"uuid-{clinic_id}", "clinic_name": f"Clinic {clinic_id}",
            "clinic_address": f"Address {clinic_id}", "listing_status": listing,
            "listing_article_url": "", "clinic_profile_url": "", "gmail_state": gmail,
            "gmail_draft_id": "", "gmail_message_id": "", "gmail_thread_id": "",
            "article_type": article_type}


def test_328_clinic_preview_reproduces_declared_listing_totals_without_writing():
    rows = ([_row(i, "LISTED", "NORMAL") for i in range(1, 20)]
            + [_row(i, "NOT_LISTED") for i in range(20, 117)]
            + [_row(i, "LISTED", "PR") for i in range(117, 120)]
            + [_row(120, "LISTED", "SUPERVISED")]
            + [_row(i, "UNCONFIRMED") for i in range(121, 329)])
    parsed = parse_legacy_import_csv(_csv(rows).encode())
    preview = preview_legacy_import(parsed)
    assert preview.total_rows == preview.unique_clinic_ids == preview.unique_clinic_uuids == 328
    assert preview.expected_count_matches and preview.status_count_matches
    assert preview.listing_status_counts == {
        "LISTED": 19, "NOT_LISTED": 97, "PR": 3, "SUPERVISED": 1, "UNCONFIRMED": 208,
    }


def test_import_requires_canonical_columns_and_reports_duplicates():
    with pytest.raises(LegacyImportInputError):
        parse_legacy_import_csv("clinic_id,clinic_name\n1,Clinic\n")
    rows = [_row(1, "NOT_LISTED"), _row(1, "NOT_LISTED"), _row(2, "UNCONFIRMED")]
    preview = preview_legacy_import(parse_legacy_import_csv(_csv(rows)))
    assert preview.duplicate_clinic_ids == 1
    assert preview.expected_count_matches is False


def test_snapshot_comparison_uses_only_explicit_id_uuid_name_address():
    row = parse_legacy_import_csv(_csv([_row(1, "NOT_LISTED")]))
    compared = compare_clinic_snapshot(row, [{"id": 1, "uuid": "wrong", "clinic_name": "Other", "address": "Elsewhere"}])
    assert compared == {"missing_clinic_ids": 0, "uuid_mismatches": 1,
                        "name_mismatches": 1, "address_mismatches": 1}
