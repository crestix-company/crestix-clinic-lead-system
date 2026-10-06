"""Repository contracts (Protocols), derived from how app_v2.py / src/master/store.py and the
sidecar modules (hp_effective_rank, hp_site_type, hp_batch_metrics, research_sidecar) are used
today. Each Protocol maps to one existing schema/concern -- no God Repository.

These are structural (typing.Protocol): SqliteXxxRepository classes satisfy them by delegating
to the existing, unmodified ClinicStore / sidecar functions; SupabaseXxxRepository classes
satisfy them by querying the migrated Postgres schemas. Both sides are checked by the dual
backend parity harness (scripts/supabase_migration/parity_harness.py).
"""
from typing import Protocol, Any


class ClinicRepository(Protocol):
    """public.clinics / clinics (SQLite) -- clinic master read (+ existing write path)."""

    def get(self, clinic_id: int) -> dict: ...

    def get_by_uuid(self, uuid: str) -> dict | None: ...

    def get_by_medical_key(self, medical_key: str) -> dict | None: ...

    def query(self, filters=None, limit: int = 100, offset: int = 0, as_of=None) -> list[dict]: ...

    def count(self, filters=None, as_of=None) -> int: ...

    def funnel(self, filters, as_of=None) -> list[tuple[str, int]]: ...

    def metrics(self) -> dict[str, int]: ...


class HpResearchRepository(Protocol):
    """hp_research.clinic_hp_research / HP batch sidecar -- machine rank, URLs, batch metrics.

    Deliberately I/O-only: effective_hp_rank()/effective_rank_reason()/classify_site() stay pure
    functions in src.master.hp_effective_rank / src.master.hp_site_type, imported and called
    identically by both adapters so there is exactly one implementation of that business logic.
    """

    def machine_rank(self, clinic_id: int) -> str: ...

    def batch_urls(self, clinic_id: int) -> tuple[str, str]: ...

    def website_treatment_categories(self, clinic_id: int) -> list[str]: ...

    def batch_metrics(self) -> dict[str, int] | None: ...


class TreatmentRepository(Protocol):
    """treatment.clinic_research_status / treatment.clinic_treatment_research."""

    def confirmed_categories(self, clinic_id: int) -> list[str]: ...

    def status_for_ids(self, ids: list[int]) -> dict[int, str]: ...

    def status_counts(self, filters=None, as_of=None) -> dict[str, int] | None: ...


class ResearchRepository(Protocol):
    """research.research_results / research.hp_pages (read-only in Stage4-A)."""

    def research_result(self, clinic_id: int) -> dict: ...

    def hp_pages(self, clinic_id: int) -> list[dict]: ...


class ProvenanceRepository(Protocol):
    """provenance.change_history / provenance.templates / provenance.match_reviews."""

    def history_for_clinic(self, clinic_id: int, limit: int = 30) -> list[dict]: ...

    def templates(self) -> list[dict]: ...

    def reviews(self, limit: int = 100) -> list[dict]: ...


class SettingsRepository(Protocol):
    """app_config.settings / settings."""

    def get(self, key: str, default: Any = None) -> Any: ...

    def set(self, key: str, value: Any) -> None: ...
