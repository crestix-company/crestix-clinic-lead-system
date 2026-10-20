"""Cloud Run-specific safety wrapper around the existing, proven
src.repository.*.import_master() write path, mirroring
src.master.comdesk_cloud_import's pattern exactly for the same reasons (see
that module's docstring). Reuses load_master()/import_master() business
logic as-is.

Known gap, same as Comdesk: this preview shows row/column counts and raw
file content only, not a new/updated/unchanged outcome breakdown --
import_master()'s matching semantics are not something to re-derive for a
dry run under time pressure. Documented here and surfaced in the UI rather
than guessed at.
"""
from __future__ import annotations

import hashlib
import json


def kouseikyoku_import_idempotency_key(file_bytes: bytes, *, mode: str, prefecture: str, sheet) -> str:
    """Stable across reruns for the identical (file, mode, prefecture, sheet)
    combination -- changing any of them produces a new key."""
    normalized = json.dumps(
        {"mode": mode, "prefecture": prefecture, "sheet": sheet}, sort_keys=True, default=str,
    )
    digest = hashlib.sha256()
    digest.update(file_bytes)
    digest.update(b"\0")
    digest.update(normalized.encode("utf-8"))
    return digest.hexdigest()


def preview_kouseikyoku_import(frame, *, preview_rows=5):
    """Read-only: never calls any write path. frame is the already-parsed
    DataFrame load_master() returns."""
    return {
        "row_count": len(frame),
        "column_count": len(frame.columns),
        "preview": frame.head(preview_rows).copy(),
    }
