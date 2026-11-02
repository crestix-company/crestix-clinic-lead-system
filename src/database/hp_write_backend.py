"""HP WRITE backend factory; business workers never select adapters directly."""
import os

from src.database.write_backend import active_write_backend, WRITE_BACKEND_SQLITE


def build_hp_write_repository(sqlite_connection):
    if active_write_backend() == WRITE_BACKEND_SQLITE:
        from src.database.hp_sqlite_write_adapter import SqliteHpWriteRepository
        return SqliteHpWriteRepository(sqlite_connection)
    from src.database.supabase_adapter import connect
    from src.database.hp_supabase_write_adapter import SupabaseHpWriteRepository
    url = os.environ.get("SUPABASE_RUNTIME_DB_URL")
    if not url:
        raise RuntimeError("SUPABASE_RUNTIME_DB_URL is not set")
    return SupabaseHpWriteRepository(connect(url, autocommit=False))
