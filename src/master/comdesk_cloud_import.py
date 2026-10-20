"""Cloud Run-specific safety wrapper around the existing, proven
src.repository.*.import_comdesk() write path (see its own docstring: UUID
matching, provenance, and "import-batch idempotency" are already preserved
at the data layer -- re-running the same file twice re-matches rather than
duplicates). What's missing for a hypothetical future Cloud Run Writer
deployment (NOT the current READ ONLY Canary, which this never runs under --
see app_v2.py's CLINIC_CLOUD_COMDESK_IMPORT_ENABLED gate) is the operator-
facing layer local Mac usage never needed: an explicit preview/confirm split
and a same-session double-submit guard, on top of an identified operator
(src/cloud_run/operator_identity.py) -- because unlike a single trusted local
user, a Cloud Run deployment can be reached by any IAP-authorized user.

Issue #130 P1: deliberately small, not a generic framework. This module adds
exactly two things beyond what import_comdesk() already guarantees:
  1. A stable idempotency key per (file content, column mapping) pair, so the
     UI can refuse to silently re-apply the same submission twice in one
     session (defense in depth on top of import_comdesk()'s own data-level
     idempotency).
  2. A preview step that never calls the write path, so an operator can see
     what mapping/row count will be used before confirming.
"""
from __future__ import annotations

import hashlib
import json


def comdesk_import_idempotency_key(file_bytes: bytes, mapping: dict) -> str:
    """Stable across reruns for the identical (file, mapping) pair -- changing
    either the uploaded file or any column mapping choice produces a new key,
    so a genuinely different submission is never blocked by a stale one."""
    normalized_mapping = json.dumps(mapping or {}, sort_keys=True, default=str)
    digest = hashlib.sha256()
    digest.update(file_bytes)
    digest.update(b"\0")
    digest.update(normalized_mapping.encode("utf-8"))
    return digest.hexdigest()


def preview_comdesk_import(table, mapping, *, preview_rows=5):
    """Read-only: never calls any write path. Returns a small summary dict
    safe to render -- row count and the resolved column mapping only, no
    row-level clinic data beyond the same bounded preview the existing local
    import_ui_comdesk_step() already shows (head(preview_rows))."""
    return {
        "row_count": len(table.data),
        "column_count": len(table.headers),
        "mapping": dict(mapping or {}),
        "preview": table.data.head(preview_rows).copy(),
    }
