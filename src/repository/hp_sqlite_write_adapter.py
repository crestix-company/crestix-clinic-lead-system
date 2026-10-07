"""SQLite implementation of HpWriteRepository -- thin delegation to the existing, unmodified
src.master.hp_research_batch.upsert_result(). Zero behavior change: same sidecar connection the
worker already manages, same SQL, same idempotent ON CONFLICT(clinic_id) DO UPDATE semantics.
"""


class SqliteHpWriteRepository:
    def __init__(self, sidecar_conn):
        self._conn = sidecar_conn

    def upsert_result(self, result):
        from src.master.hp_research_batch import upsert_result
        upsert_result(self._conn, result)
