"""Stage4-D Gate2-A: WRITE backend selection, independent of CLINIC_DATA_BACKEND (READ).

Stage4-D cut production writes over to Supabase through the tracked production configuration.
SQLite remains reachable only through an explicit admin/migration/test configuration.

No silent fallback: unlike the READ comparator in src.repository.cutover, a Supabase WRITE
failure must never cause an automatic SQLite write of the same operation (dual-write stays
forbidden). Callers that need rollback simply set CLINIC_WRITE_BACKEND=sqlite explicitly.
"""
import os

WRITE_BACKEND_ENV_VAR = "CLINIC_WRITE_BACKEND"
WRITE_BACKEND_SQLITE = "sqlite"
WRITE_BACKEND_SUPABASE = "supabase"
VALID_WRITE_BACKENDS = (WRITE_BACKEND_SQLITE, WRITE_BACKEND_SUPABASE)
RUNTIME_DB_URL_ENV_VAR = "SUPABASE_RUNTIME_DB_URL"


def active_write_backend():
    value = (os.environ.get(WRITE_BACKEND_ENV_VAR) or WRITE_BACKEND_SQLITE).strip().lower()
    if value not in VALID_WRITE_BACKENDS:
        raise ValueError(
            f"{WRITE_BACKEND_ENV_VAR}は{VALID_WRITE_BACKENDS}のいずれかにしてください（指定値: {value!r}）。"
        )
    return value


def build_write_repositories(backend=None, *, sqlite_path=None, supabase_url=None):
    """Factory: returns a WriteRepositories bundle for the requested (or env-selected) backend.
    Mirrors src.repository.backend.build_repositories()'s shape for the READ side.
    """
    from src.repository.write_contracts import WriteRepositories

    backend = backend or active_write_backend()
    if backend == WRITE_BACKEND_SQLITE:
        from src.master.store import ClinicStore
        from src.master.data_paths import production_db_path
        from src.repository.sqlite_write_adapter import (
            SqliteClinicWriteRepository, SqliteResearchWriteRepository,
            SqliteProvenanceWriteRepository, SqliteSettingsWriteRepository,
            SqliteJobsWriteRepository, SqliteSearchWriteRepository,
        )
        store = ClinicStore(sqlite_path or production_db_path())
        return WriteRepositories(
            clinics=SqliteClinicWriteRepository(store),
            research=SqliteResearchWriteRepository(store),
            provenance=SqliteProvenanceWriteRepository(store),
            settings=SqliteSettingsWriteRepository(store),
            jobs=SqliteJobsWriteRepository(store),
            search=SqliteSearchWriteRepository(store),
        )
    if backend == WRITE_BACKEND_SUPABASE:
        import os as _os
        from src.repository.supabase_adapter import connect
        from src.repository.supabase_write_adapter import (
            SupabaseClinicWriteRepository, SupabaseResearchWriteRepository,
            SupabaseProvenanceWriteRepository, SupabaseSettingsWriteRepository,
            SupabaseJobsWriteRepository, SupabaseSearchWriteRepository,
        )
        url = supabase_url or _os.environ.get(RUNTIME_DB_URL_ENV_VAR)
        if not url:
            raise RuntimeError(f"{RUNTIME_DB_URL_ENV_VAR} is not set")
        # WRITE connections stay in default (non-autocommit) mode: every write path manages its
        # own explicit transaction boundary (see supabase_write_adapter), unlike the READ side's
        # long-lived autocommit connection in src.repository.backend.
        conn = connect(url, autocommit=False)
        return WriteRepositories(
            clinics=SupabaseClinicWriteRepository(conn),
            research=SupabaseResearchWriteRepository(conn),
            provenance=SupabaseProvenanceWriteRepository(conn),
            settings=SupabaseSettingsWriteRepository(conn),
            jobs=SupabaseJobsWriteRepository(conn),
            search=SupabaseSearchWriteRepository(conn),
        )
    raise AssertionError(backend)


def write_repositories_for(store):
    """Runtime call-site entry point for app_v2.py/jobs.py/search_provider.py/google_maps.py.

    Unlike build_write_repositories(), the sqlite path here wraps the CALLER'S OWN existing
    ClinicStore instance (e.g. app_v2.py's @st.cache_resource `store_for(path)` result) instead
    of constructing a brand-new one from production_db_path(). This matters: a fresh ClinicStore
    could theoretically resolve a different path than the session's already-resolved store
    (demo mode, CLINIC_DEMO_DB_PATH, custom path argument) -- reusing the exact same object
    makes CLINIC_WRITE_BACKEND=sqlite (the default) provably behavior-identical to calling the
    store's methods directly, not just "probably the same DB file." `store` may be a
    Stage4CClinicStore/ShadowClinicStore READ wrapper too: both delegate every attribute they
    don't override (which is every WRITE method) straight through to the inner ClinicStore, so
    wrapping the wrapper here is equivalent to wrapping the inner store.

    The Supabase path ignores `store` entirely and opens its own connection (there is no
    "existing Supabase object" to reuse) -- selecting CLINIC_WRITE_BACKEND=supabase is itself
    the live-cutover action this function does not perform on its own.
    """
    from src.repository.write_contracts import WriteRepositories

    if active_write_backend() == WRITE_BACKEND_SQLITE:
        from src.repository.sqlite_write_adapter import (
            SqliteClinicWriteRepository, SqliteResearchWriteRepository,
            SqliteProvenanceWriteRepository, SqliteSettingsWriteRepository,
            SqliteJobsWriteRepository, SqliteSearchWriteRepository,
        )
        return WriteRepositories(
            clinics=SqliteClinicWriteRepository(store),
            research=SqliteResearchWriteRepository(store),
            provenance=SqliteProvenanceWriteRepository(store),
            settings=SqliteSettingsWriteRepository(store),
            jobs=SqliteJobsWriteRepository(store),
            search=SqliteSearchWriteRepository(store),
        )
    return build_write_repositories()
