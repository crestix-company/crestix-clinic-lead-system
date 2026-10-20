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
functions, plus the same classify_match_status()/build_maps_update() pure
functions import_maps_results() itself calls, in a read-only pass -- SELECT-
only, never any INSERT/UPDATE, always rolled back even if something raises
partway through -- so the preview returns the exact same counts shape
(TOTAL/MATCHED/WEBSITE/NO_WEBSITE/NOT_FOUND/AMBIGUOUS/EXCLUDED/ERROR/
UNLINKED/PRESERVED_WEBSITE) that import_maps_results()'s own return value
uses after the real write. Review finding: an earlier draft of this preview
used a different, narrower vocabulary (NEW/MATCHED/AMBIGUOUS/EXCLUDED/
ERROR) where "MATCHED" meant "an existing clinic was found" -- but the real
import's own "MATCHED" means something else entirely ("the collector found
a Maps profile for this business", WEBSITE+NO_WEBSITE combined, completely
independent of whether an existing clinic matched). Showing that narrower
vocabulary next to the real result's own counts would have let an operator
misread one "MATCHED" as the other. Using the identical shape removes that
ambiguity by construction, and also means the preview is still explicitly
labeled an estimate (see preview_maps_import's docstring) because a
concurrent write between preview and apply can still change which clinics
match -- only import_maps_results()'s own return value, after the real
write, is the confirmed result.
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
    """Read-only: runs the exact matching and classification logic
    import_maps_results() uses (_prefetch_maps_targets, then
    _find_target_prefetched, then classify_match_status/build_maps_update --
    all on provenance_repo, all the same pure/SELECT-only functions the real
    write below calls), and returns the identical counts shape that
    function's own return value uses: TOTAL/MATCHED/WEBSITE/NO_WEBSITE/
    NOT_FOUND/AMBIGUOUS/EXCLUDED/ERROR/UNLINKED/PRESERVED_WEBSITE.

    Never issues an INSERT/UPDATE and never commits. Every DB-touching step
    below is wrapped so that ANY exception -- a SQL error from a malformed
    or oversized batch, a matching-function exception, a mid-loop failure --
    still reaches the rollback before propagating to the caller: this
    connection must never be left with an open, uncommitted transaction, and
    must never, under any circumstance including a partial failure, commit.

    This is an ESTIMATE, not the confirmed result: a concurrent write between
    this preview and the later apply step (e.g. another operator's import,
    or a merge) can change which clinics a row ultimately matches. Callers
    must label this preview as such and never treat its counts as the
    applied outcome -- only import_maps_results()'s own return value, after
    the real write, is the confirmed result.
    """
    from src.master.google_maps import validate_maps_frame, classify_match_status, build_maps_update, _s

    frame = validate_maps_frame(frame)
    records = frame.to_dict("records")
    counts = {
        "TOTAL": len(records), "MATCHED": 0, "WEBSITE": 0, "NO_WEBSITE": 0,
        "NOT_FOUND": 0, "AMBIGUOUS": 0, "EXCLUDED": 0, "ERROR": 0,
        "UNLINKED": 0, "PRESERVED_WEBSITE": 0,
    }
    try:
        with provenance_repo._conn.cursor() as cur:
            target_indexes = provenance_repo._prefetch_maps_targets(cur, records)
            matched = []
            for row in records:
                cid, method, _score = provenance_repo._find_target_prefetched(row, target_indexes)
                matched.append((row, cid, method))
                maps_status = _s(row.get("maps_match_status"))
                bucket = classify_match_status(maps_status)
                if bucket in ("WEBSITE", "NO_WEBSITE"):
                    counts["MATCHED"] += 1
                    counts[bucket] += 1
                elif bucket is not None:
                    counts[bucket] += 1
                if cid is None:
                    counts["UNLINKED"] += 1

            clinic_ids = sorted({cid for _row, cid, _method in matched if cid is not None})
            base_by_id = {}
            if clinic_ids:
                cur.execute(
                    "SELECT id, base_json FROM public.clinics WHERE id = ANY(%s::bigint[])",
                    (clinic_ids,),
                )
                for clinic_id, raw_base in cur.fetchall():
                    base_by_id[int(clinic_id)] = provenance_repo._json_value(raw_base)
            for row, cid, method in matched:
                if cid is None or cid not in base_by_id:
                    continue
                maps_status = _s(row.get("maps_match_status"))
                _update, preserved = build_maps_update(row, base_by_id[cid], maps_status, method)
                if preserved:
                    counts["PRESERVED_WEBSITE"] += 1
    finally:
        # SELECT-only above; roll back unconditionally, including when the
        # try block above raised, so this preview can never leave anything
        # committed and never leaves an idle-in-transaction connection behind.
        provenance_repo._conn.rollback()
    return {
        "counts": counts,
        "row_count": len(records),
        "column_count": len(frame.columns),
        "preview": frame.head(preview_rows).copy(),
    }
