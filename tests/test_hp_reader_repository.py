from types import SimpleNamespace

from src.master.jobs import job_status, recent_jobs
from src.repository.runtime_store import SupabaseRuntimeStore


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.description = [SimpleNamespace(name="id"), SimpleNamespace(name="status")]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        self.connection.queries.append((query, params))

    def fetchone(self):
        return ("job-1", "PAUSED")

    def fetchall(self):
        query = self.connection.queries[-1][0]
        if "GROUP BY state" in query:
            return [("PENDING", 2), ("DONE", 1)]
        if "GROUP BY result" in query:
            return [("SUCCESS", 1)]
        return [("job-1", "PAUSED")]


class FakeConnection:
    closed = False
    broken = False

    def __init__(self):
        self.queries = []

    def cursor(self):
        return FakeCursor(self)


def test_hp_job_reads_use_reader_store_methods_only():
    conn = FakeConnection()
    store = SupabaseRuntimeStore(SimpleNamespace(clinics=SimpleNamespace(_conn=conn)))

    jobs = recent_jobs(store)
    status = job_status(store, "job-1")

    assert jobs == [{"id": "job-1", "status": "PAUSED"}]
    assert status["counts"] == {"PENDING": 2, "DONE": 1}
    assert status["results"] == {"SUCCESS": 1}
    assert all(query.lstrip().upper().startswith("SELECT") for query, _ in conn.queries)
