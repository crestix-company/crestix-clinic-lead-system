"""Fail-closed loader for a fixed Canary cohort of clinic_ids.

Canary mode is an explicit opt-in (EMAIL_RESEARCH_COHORT_FILE set) layered on top of
normal continuous Auto Run: when unset, nothing here is reached and behavior is
byte-for-byte unchanged. When set, any validation failure raises -- there is no fallback
to the unrestricted Production target pool.
"""
from __future__ import annotations

import csv
import os


COHORT_FILE_ENV_VAR = "EMAIL_RESEARCH_COHORT_FILE"


def load_cohort(path: str, *, expected_count: int | None = None) -> frozenset[int]:
    """Parse a cohort CSV (must have a clinic_id column, e.g. the existing
    production_email_canary_100_*/cohort.csv artifacts) into a validated id set.

    Raises ValueError on: missing file, missing/unparseable clinic_id column, zero rows,
    any non-integer value, any duplicate, or (when expected_count is given) a row count
    that doesn't match it exactly. Never returns a partial or best-effort result.
    """
    if not path or not os.path.isfile(path):
        raise ValueError(f"Cohort fileが見つかりません: {path!r}")

    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None or "clinic_id" not in reader.fieldnames:
            raise ValueError(f"Cohort fileにclinic_id列がありません: {path!r}")
        raw_ids = []
        for line_number, row in enumerate(reader, start=2):
            value = (row.get("clinic_id") or "").strip()
            if not value:
                raise ValueError(f"{path}:{line_number} clinic_idが空です。")
            try:
                raw_ids.append(int(value))
            except ValueError:
                raise ValueError(f"{path}:{line_number} clinic_idが整数ではありません: {value!r}") from None

    if not raw_ids:
        raise ValueError(f"Cohort fileに行がありません: {path!r}")

    duplicates = {cid for cid in raw_ids if raw_ids.count(cid) > 1}
    if duplicates:
        raise ValueError(f"Cohort fileにclinic_id重複があります: {sorted(duplicates)}")

    if expected_count is not None and len(raw_ids) != expected_count:
        raise ValueError(
            f"Cohort fileの件数が{expected_count}件ではありません（実際: {len(raw_ids)}件）: {path!r}"
        )

    return frozenset(raw_ids)


def load_cohort_from_env(environ=None, *, expected_count: int | None = None) -> frozenset[int] | None:
    """Returns None (Canary mode inactive) only when the env var itself is unset/blank.
    Once set, every failure mode raises -- this never silently falls back to None."""
    env = os.environ if environ is None else environ
    path = (env.get(COHORT_FILE_ENV_VAR) or "").strip()
    if not path:
        return None
    return load_cohort(path, expected_count=expected_count)
