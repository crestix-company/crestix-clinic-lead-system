"""Strict, non-writing preview contract for the manually reviewed 328-clinic CSV."""
from __future__ import annotations

import csv
import io
from collections import Counter
from dataclasses import dataclass

REQUIRED_COLUMNS = frozenset({
    "clinic_id", "clinic_uuid", "clinic_name", "clinic_address", "listing_status",
    "listing_article_url", "clinic_profile_url", "gmail_state", "gmail_draft_id",
    "gmail_message_id", "gmail_thread_id",
})
LISTING_STATUSES = frozenset({"LISTED", "NOT_LISTED", "UNCONFIRMED", "ERROR"})
GMAIL_STATES = frozenset({"DRAFT", "SENT", "SCHEDULED", "UNKNOWN", "REVIEW", "NONE"})
EXPECTED_LISTING_COUNTS = {
    "LISTED": 19, "NOT_LISTED": 97, "PR": 3, "SUPERVISED": 1, "UNCONFIRMED": 208,
}


class LegacyImportInputError(ValueError):
    pass


@dataclass(frozen=True)
class LegacyImportRow:
    clinic_id: int
    clinic_uuid: str
    clinic_name: str
    clinic_address: str
    listing_status: str
    listing_article_url: str
    clinic_profile_url: str
    gmail_state: str
    gmail_draft_id: str
    gmail_message_id: str
    gmail_thread_id: str
    article_type: str = "UNKNOWN"


@dataclass(frozen=True)
class LegacyImportPreview:
    total_rows: int
    unique_clinic_ids: int
    unique_clinic_uuids: int
    duplicate_clinic_ids: int
    duplicate_clinic_uuids: int
    listing_status_counts: dict[str, int]
    gmail_state_counts: dict[str, int]
    incomplete_identity_rows: int
    expected_count_matches: bool
    status_count_matches: bool
    rows: tuple[LegacyImportRow, ...]


def parse_legacy_import_csv(data: bytes | str) -> list[LegacyImportRow]:
    text = data.decode("utf-8-sig") if isinstance(data, bytes) else data
    reader = csv.DictReader(io.StringIO(text))
    headers = set(reader.fieldnames or ())
    missing = REQUIRED_COLUMNS - headers
    if missing:
        raise LegacyImportInputError("Import preview CSV is missing canonical columns: " + ", ".join(sorted(missing)))
    rows = []
    for line, row in enumerate(reader, start=2):
        try:
            clinic_id = int((row.get("clinic_id") or "").strip())
        except ValueError as exc:
            raise LegacyImportInputError(f"Row {line}: clinic_id is invalid") from exc
        listing = (row.get("listing_status") or "").strip().upper()
        gmail = (row.get("gmail_state") or "").strip().upper()
        article_type = (row.get("article_type") or "UNKNOWN").strip().upper()
        if clinic_id <= 0 or listing not in LISTING_STATUSES or gmail not in GMAIL_STATES:
            raise LegacyImportInputError(f"Row {line}: identity or status is invalid")
        if article_type not in {"NORMAL", "PR", "SUPERVISED", "UNKNOWN"}:
            raise LegacyImportInputError(f"Row {line}: article_type is invalid")
        rows.append(LegacyImportRow(
            clinic_id=clinic_id,
            clinic_uuid=(row.get("clinic_uuid") or "").strip(),
            clinic_name=(row.get("clinic_name") or "").strip(),
            clinic_address=(row.get("clinic_address") or "").strip(),
            listing_status=listing,
            listing_article_url=(row.get("listing_article_url") or "").strip(),
            clinic_profile_url=(row.get("clinic_profile_url") or "").strip(),
            gmail_state=gmail,
            gmail_draft_id=(row.get("gmail_draft_id") or "").strip(),
            gmail_message_id=(row.get("gmail_message_id") or "").strip(),
            gmail_thread_id=(row.get("gmail_thread_id") or "").strip(),
            article_type=article_type,
        ))
    return rows


def preview_legacy_import(rows: list[LegacyImportRow]) -> LegacyImportPreview:
    ids = [row.clinic_id for row in rows]
    uuids = [row.clinic_uuid.casefold() for row in rows if row.clinic_uuid]
    id_counts, uuid_counts = Counter(ids), Counter(uuids)
    listing_counts = Counter(row.listing_status for row in rows)
    # PR and supervised are disjoint counts supplied by the trusted CSV; the canonical
    # listing_status remains LISTED and article_type carries the subtype.
    for kind in ("PR", "SUPERVISED"):
        listing_counts[kind] = sum(row.article_type == kind for row in rows)
    listing_counts["LISTED"] = sum(row.listing_status == "LISTED"
                                   and row.article_type not in {"PR", "SUPERVISED"} for row in rows)
    gmail_counts = Counter(row.gmail_state for row in rows)
    expected_counts_match = len(rows) == 328
    status_counts_match = all(listing_counts.get(key, 0) == count
                              for key, count in EXPECTED_LISTING_COUNTS.items())
    return LegacyImportPreview(
        total_rows=len(rows),
        unique_clinic_ids=len(id_counts),
        unique_clinic_uuids=len(uuid_counts),
        duplicate_clinic_ids=sum(count - 1 for count in id_counts.values() if count > 1),
        duplicate_clinic_uuids=sum(count - 1 for count in uuid_counts.values() if count > 1),
        listing_status_counts=dict(sorted(listing_counts.items())),
        gmail_state_counts=dict(sorted(gmail_counts.items())),
        incomplete_identity_rows=sum(not row.clinic_uuid or not row.clinic_name or not row.clinic_address
                                     for row in rows),
        expected_count_matches=expected_counts_match,
        status_count_matches=status_counts_match,
        rows=tuple(rows),
    )


def compare_clinic_snapshot(rows: list[LegacyImportRow], clinic_rows: list[dict]) -> dict[str, int]:
    """Compare explicit ID/UUID/name/address fields only; never fuzzy-match or write."""
    clinics = {int(row.get("id") or row.get("clinic_id") or 0): row for row in clinic_rows}
    missing_clinic_ids = 0
    uuid_mismatches = name_mismatches = address_mismatches = 0
    for row in rows:
        current = clinics.get(row.clinic_id)
        if current is None:
            missing_clinic_ids += 1
            continue
        if row.clinic_uuid and (str(current.get("uuid") or "").casefold() != row.clinic_uuid.casefold()):
            uuid_mismatches += 1
        if row.clinic_name and str(current.get("clinic_name") or "").strip() != row.clinic_name:
            name_mismatches += 1
        address = str(current.get("address") or "").strip()
        if row.clinic_address and address != row.clinic_address:
            address_mismatches += 1
    return {
        "missing_clinic_ids": missing_clinic_ids,
        "uuid_mismatches": uuid_mismatches,
        "name_mismatches": name_mismatches,
        "address_mismatches": address_mismatches,
    }


__all__ = ["LegacyImportInputError", "LegacyImportRow", "LegacyImportPreview",
           "parse_legacy_import_csv", "preview_legacy_import", "compare_clinic_snapshot",
           "REQUIRED_COLUMNS", "EXPECTED_LISTING_COUNTS"]
