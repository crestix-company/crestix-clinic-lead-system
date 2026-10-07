"""Treatment WRITE backend factory; business workers never select adapters directly."""
import os

from src.repository.write_backend import active_write_backend, WRITE_BACKEND_SQLITE


def build_treatment_write_repository(sqlite_connection):
    if active_write_backend() == WRITE_BACKEND_SQLITE:
        from src.repository.treatment_sqlite_write_adapter import SqliteTreatmentWriteRepository
        return SqliteTreatmentWriteRepository(sqlite_connection)
    from src.repository.supabase_adapter import connect
    from src.repository.treatment_supabase_write_adapter import SupabaseTreatmentWriteRepository
    url = os.environ.get("SUPABASE_RUNTIME_DB_URL")
    if not url:
        raise RuntimeError("SUPABASE_RUNTIME_DB_URL is not set")
    return SupabaseTreatmentWriteRepository(connect(url, autocommit=False))
