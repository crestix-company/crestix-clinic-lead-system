"""Backend selection for runtime reads.

Stage4-C makes Supabase the default READ backend.  SQLite remains the only WRITE backend and
``CLINIC_DATA_BACKEND=sqlite`` is the immediate rollback switch.
"""
import os
from dataclasses import dataclass

BACKEND_ENV_VAR = "CLINIC_DATA_BACKEND"
BACKEND_SQLITE = "sqlite"
BACKEND_SUPABASE = "supabase"
VALID_BACKENDS = (BACKEND_SQLITE, BACKEND_SUPABASE)


def active_backend():
    value = (os.environ.get(BACKEND_ENV_VAR) or BACKEND_SUPABASE).strip().lower()
    if value not in VALID_BACKENDS:
        raise ValueError(f"{BACKEND_ENV_VAR}は{VALID_BACKENDS}のいずれかにしてください（指定値: {value!r}）。")
    return value


@dataclass
class Repositories:
    clinics: object
    hp_research: object
    treatment: object
    research: object
    provenance: object
    settings: object


def build_repositories(backend=None, *, sqlite_path=None, supabase_url=None):
    """Factory: returns a Repositories bundle for the requested (or env-selected) backend.
    Opens exactly one connection/store per call; caller owns its lifetime.
    """
    backend = backend or active_backend()
    if backend == BACKEND_SQLITE:
        from src.master.store import ClinicStore
        from src.master.data_paths import production_db_path
        from src.repository.sqlite_adapter import (
            SqliteClinicRepository, SqliteHpResearchRepository, SqliteTreatmentRepository,
            SqliteResearchRepository, SqliteProvenanceRepository, SqliteSettingsRepository,
        )
        store = ClinicStore(sqlite_path or production_db_path())
        return Repositories(
            clinics=SqliteClinicRepository(store),
            hp_research=SqliteHpResearchRepository(),
            treatment=SqliteTreatmentRepository(store),
            research=SqliteResearchRepository(store),
            provenance=SqliteProvenanceRepository(store),
            settings=SqliteSettingsRepository(store),
        )
    if backend == BACKEND_SUPABASE:
        import os as _os
        from src.repository.supabase_adapter import (
            connect, SupabaseClinicRepository, SupabaseHpResearchRepository, SupabaseTreatmentRepository,
            SupabaseResearchRepository, SupabaseProvenanceRepository, SupabaseSettingsRepository,
        )
        url = supabase_url or _os.environ.get("SUPABASE_DB_URL")
        if not url:
            # Runtime cutover catches ordinary exceptions and falls back to SQLite.  SystemExit
            # would terminate Streamlit's script thread and bypass that safety mechanism.
            raise RuntimeError("SUPABASE_DB_URL is not set")
        # Stage4-C runtime connections are long-lived.  Autocommit keeps one failed/cancelled
        # SELECT from poisoning every later read with InFailedSqlTransaction.
        conn = connect(url, autocommit=True)
        hp = SupabaseHpResearchRepository(conn)
        return Repositories(
            clinics=SupabaseClinicRepository(conn, hp),
            hp_research=hp,
            treatment=SupabaseTreatmentRepository(conn),
            research=SupabaseResearchRepository(conn),
            provenance=SupabaseProvenanceRepository(conn),
            settings=SupabaseSettingsRepository(conn),
        )
    raise AssertionError(backend)
