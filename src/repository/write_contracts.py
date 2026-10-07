"""Stage4-D Gate2-A WRITE Repository contracts (Protocols).

Mirrors the shape of src/repository/contracts.py (READ side): each Protocol maps to one
Gate 1.5 mandatory (Class A/B) write concern. SqliteXxxWriteRepository classes satisfy these by
delegating to the existing, unmodified ClinicStore/jobs.py/search_provider.py/google_maps.py
functions (zero behavior change). SupabaseXxxWriteRepository classes satisfy them by writing to
the migrated Postgres schemas under the Owner Decision 1/2 contracts.

Every write method raises on failure; none of them catch-and-fallback to the other backend
(see src.repository.write_backend's module docstring -- no silent WRITE fallback, no dual-write).
"""
from dataclasses import dataclass
from typing import Protocol, Any


@dataclass
class WriteRepositories:
    clinics: object
    research: object
    provenance: object
    settings: object
    jobs: object
    search: object


class ClinicWriteRepository(Protocol):
    """public.clinics / clinics -- the Owner Decision 1/2 identity-sensitive write surface."""

    def insert_new_clinic(self, record: dict, *, is_new: bool, is_authoritative_official_source: bool) -> int:
        """INSERT a brand-new clinic row. Returns the generated id (Supabase: RETURNING id,
        guarded by src.repository.high_range_id; SQLite: lastrowid, unguarded, unchanged).
        medical_key is generated only here, only from an authoritative official source.
        """
        ...

    def update_matched_clinic_base(self, clinic_id: int, record: dict, *, source: str, as_of: str) -> None:
        """MATCHED-import base update (store.py:_upsert MATCHED branch semantics)."""
        ...

    def refresh_projection(self, clinic_id: int, *, is_authoritative_official_source: bool = False) -> None:
        """Recompute non-identity projection columns. medical_key is validated via
        resolve_medical_key_transition and only ever included in the UPDATE when that call
        returns a value different from the stored one AND authoritative completion applied.
        """
        ...

    def resolve_review_to_new_clinic(self, record: dict, *, is_authoritative_official_source: bool) -> int:
        ...

    def resolve_review_to_existing_clinic(self, clinic_id: int, record: dict, *, source: str) -> None:
        ...


class ResearchWriteRepository(Protocol):
    """research.research_results / research.hp_pages / provenance.manual_overrides."""

    def save_research(self, clinic_id: int, result: dict, pages: list[dict] | None = None) -> None: ...

    def override(self, clinic_id: int, field: str, value: Any, *, note: str = "", source: str = "手動確認") -> None: ...


class ProvenanceWriteRepository(Protocol):
    """provenance.source_records / provenance.comdesk_original_rows / provenance.match_reviews
    / provenance.change_history / provenance.google_maps_results."""

    def insert_source_record(self, clinic_id: int | None, source: str, record: dict, source_hash: str,
                              row_number: int, match_status: str, match_reason: str, match_score: float) -> int: ...

    def history(self, clinic_id: int, action: str, before: Any, after: Any, note: str = "") -> None: ...

    def import_maps_results(self, clinic_id: int, result: dict, batch_hash: str) -> None: ...


class SettingsWriteRepository(Protocol):
    """app_config.settings / settings -- ON CONFLICT(key) DO UPDATE target."""

    def set(self, key: str, value: Any) -> None: ...


class JobsWriteRepository(Protocol):
    """research.research_jobs / research.research_job_items lifecycle."""

    def create_job(self, clinic_ids: list[int], kind: str, options: dict, max_searches: int) -> str: ...

    def pause_job(self, job_id: str) -> None: ...

    def reset_job(self, job_id: str) -> None: ...

    def job_limit(self, job_id: str, limit: int) -> None: ...

    def claim_next_pending_item(self, job_id: str) -> int | None:
        """Atomic claim: PENDING -> RUNNING for exactly one clinic_id, or None if empty.
        Supabase: single UPDATE ... RETURNING clinic_id (no BEGIN IMMEDIATE needed -- the row
        lock from UPDATE is the equivalent atomicity boundary). SQLite: unchanged BEGIN IMMEDIATE.
        """
        ...

    def mark_item_state(self, job_id: str, clinic_id: int, state: str, *, result: str = None, note: str = "") -> None: ...

    def mark_job_status(self, job_id: str, status: str) -> None: ...


class SearchWriteRepository(Protocol):
    """research.search_usage / research.search_cache -- CachedSearch's two-phase contract."""

    def reserve_attempt(self, job_id: str | None, query_key: str, month: str) -> None:
        """Transaction 1: commits BEFORE any external HTTP call. A failed/charged external
        call is never refunded (matches current SQLite behavior exactly)."""
        ...

    def store_cache_result(self, query_key: str, query: str, result: list[dict]) -> None:
        """Transaction 2: commits AFTER a successful external call. ON CONFLICT(query_key)
        DO UPDATE -- idempotent under retry."""
        ...

    def get_cached(self, query_key: str) -> list[dict] | None: ...

    def monthly_usage_count(self, month: str) -> int: ...
