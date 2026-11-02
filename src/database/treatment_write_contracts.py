"""Stage4-D Gate2: Treatment worker (scripts/research_worker.py) WRITE contract.

Scope note: the worker uses THREE separate SQLite databases (see research_worker.py's module
docstring). Only ONE of them -- the shared final results DB (default
~/CrestixData/clinic-lead/treatment_research_final.sqlite3, override TREATMENT_RESEARCH_DB_PATH)
-- maps to the 19-table Supabase migration (treatment.clinic_research_status /
treatment.clinic_treatment_research, per scripts/supabase_migration/full_shadow_import.py SPECS).

The other two (cache.sqlite3, progress.sqlite3) are per-manifest, gitignored, local-only
checkpoint/resume/dedup infrastructure -- explicitly flagged as out of the 19-table target in
the Gate 1.5 inventory ("operational state outside the 19-table target still needs an explicit
Stage4-D destination/retention contract"; not business source-of-truth data). They are NOT
Repositoryized here; only the shared final-results write is.
"""
from typing import Protocol


class TreatmentWriteRepository(Protocol):
    def write_clinic_result(self, clinic_id: int, rows: list[dict], status_row: dict) -> None:
        """One clinic, one atomic operation -- matches
        scripts.research_worker.write_clinic_atomic() exactly:
          - status_row["research_status"] == "DONE": clinic_treatment_research is fully
            replaced for this clinic_id (DELETE then INSERT every row in `rows`) -- never a
            merge/upsert, so a stale category from a previous attempt cannot survive.
          - status_row["research_status"] == "FETCH_FAILED": clinic_treatment_research is left
            completely untouched for this clinic_id (a transient failure must never delete or
            overwrite a prior successful result).
          - clinic_research_status is always upserted (ON CONFLICT DO UPDATE / INSERT OR REPLACE).
        Both writes are one transaction; rows is ignored when status is FETCH_FAILED.
        """
        ...
