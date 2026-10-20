"""Cloud Run-specific safety wrapper around the existing, proven
src.repository.supabase_write_adapter.SupabaseProvenanceWriteRepository.
import_maps_results() write path (Postgres port of src.master.google_maps.
import_maps_results() -- see that module's docstring: UUID preservation,
"confirmed website never downgraded" anti-regression, and import-batch
idempotency are already guaranteed at the data layer, unchanged here).

Mirrors src.master.comdesk_cloud_import's shape (idempotency key + preview),
but goes one step further for the preview: unlike Comdesk's SQLite-only
matcher (src.master.matching.match_record(), not safe against Postgres --
see cloud_comdesk_import_ui()'s documented KNOWN GAP), the Google Maps
matcher is already Postgres-native (SupabaseProvenanceWriteRepository.
_prefetch_maps_targets / _find_target_prefetched, the exact functions the
real write below calls). preview_maps_import() reuses those same two
functions in a read-only pass -- SELECT-only prefetch, then pure-Python
matching, never any INSERT/UPDATE -- so the preview's NEW/MATCHED/AMBIGUOUS/
EXCLUDED/ERROR breakdown is the real matching outcome, not a guess, while
still being explicitly labeled an estimate (see preview_maps_import's
docstring) because a concurrent write between preview and apply can still
change which clinics match.
"""
from __future__ import annotations

import hashlib


def maps_import_idempotency_key(file_bytes: bytes) -> str:
    """Stable across reruns for the identical file -- a same-session
    double-submit UX guard, on top of import_maps_results()'s own
    data-level batch-hash idempotency (which is the real safety boundary:
    this session guard only avoids an unnecessary re-submit within one
    browser tab, it does not prevent duplication across sessions/restarts/
    multiple users)."""
    digest = hashlib.sha256()
    digest.update(file_bytes)
    return digest.hexdigest()


def preview_maps_import(provenance_repo, frame, *, preview_rows=5):
    """Read-only: runs the exact matching logic import_maps_results() uses
    (_prefetch_maps_targets then _find_target_prefetched on provenance_repo),
    but only ever issues SELECTs and always rolls back before returning --
    never INSERT/UPDATE, never commit. Classifies every row into exactly one
    of NEW (no existing clinic matched -- would be recorded unlinked)/
    MATCHED (an existing clinic matched)/AMBIGUOUS (multiple candidate
    clinics, could not disambiguate)/EXCLUDED (hospital/center per the
    collector's own maps_match_status)/ERROR (collector reported a scrape
    error for that row), using classify_match_status() for the EXCLUDED/
    ERROR buckets (collector-side classification) and _find_target_prefetched
    for clinic-match outcome, matching import_maps_results()'s own bucket
    priority exactly (EXCLUDED/ERROR checked first, same as that function).

    This is an ESTIMATE, not the confirmed result: a concurrent write between
    this preview and the later apply step (e.g. another operator's import,
    or a merge) can change which clinics a row ultimately matches. Callers
    must label this preview as such and never treat its counts as the
    applied outcome -- only import_maps_results()'s own return value, after
    the real write, is the confirmed result.
    """
    from src.master.google_maps import validate_maps_frame, classify_match_status, _s

    frame = validate_maps_frame(frame)
    records = frame.to_dict("records")
    counts = {
        "TOTAL": len(records), "NEW": 0, "MATCHED": 0,
        "AMBIGUOUS": 0, "EXCLUDED": 0, "ERROR": 0,
    }
    with provenance_repo._conn.cursor() as cur:
        target_indexes = provenance_repo._prefetch_maps_targets(cur, records)
        for row in records:
            maps_status = _s(row.get("maps_match_status"))
            bucket = classify_match_status(maps_status)
            if bucket == "EXCLUDED":
                counts["EXCLUDED"] += 1
                continue
            if bucket == "ERROR":
                counts["ERROR"] += 1
                continue
            cid, method, _score = provenance_repo._find_target_prefetched(row, target_indexes)
            if cid is not None:
                counts["MATCHED"] += 1
            elif method == "AMBIGUOUS":
                counts["AMBIGUOUS"] += 1
            else:
                counts["NEW"] += 1
    # SELECT-only above; roll back explicitly (outside the cursor's own
    # with-block, matching import_maps_results()'s own commit/rollback
    # placement) so this preview can never leave anything committed, and
    # never leaves an idle-in-transaction connection behind either.
    provenance_repo._conn.rollback()
    return {
        "counts": counts,
        "row_count": len(records),
        "column_count": len(frame.columns),
        "preview": frame.head(preview_rows).copy(),
    }
