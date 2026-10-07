"""Stage4-D Gate2: HP research batch worker (src/master/hp_research_batch.py) WRITE contract.

Only one table is in scope: hp_research_batch_results -> hp_research.clinic_hp_research (per
scripts/supabase_migration/full_shadow_import.py SPECS). The worker's Production DB / Treatment
sidecar reads stay READ ONLY (unchanged, not a write concern) -- see select_candidates() in
hp_research_batch.py, which this Repository does not touch.
"""
from typing import Protocol


class HpWriteRepository(Protocol):
    def upsert_result(self, result: dict) -> None:
        """clinic_id-keyed idempotent upsert (attempts = prior_attempts + 1), matching
        src.master.hp_research_batch.upsert_result() exactly. `result` has the same keys
        process_one()/run_batch() already produce (see that module)."""
        ...
