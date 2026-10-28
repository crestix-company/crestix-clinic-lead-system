import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "check_mac_hp_reader", ROOT / "scripts/check_mac_hp_reader.py"
)
acceptance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acceptance)


class Cursor:
    def __init__(self):
        self.sql = []
        self.answers = iter([(4,), (29,)])

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, _params=None):
        self.sql.append(" ".join(query.split()))

    def fetchone(self):
        return next(self.answers)


class Connection:
    def __init__(self):
        self.cur = Cursor()
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self.cur

    def rollback(self):
        self.rolled_back = True


def test_reader_acceptance_checks_visible_clinics_jobs_and_items_without_job_ids(monkeypatch):
    monkeypatch.setattr(acceptance, "load_reader_dsn", lambda _path: "opaque-reader-dsn")
    monkeypatch.setattr(acceptance, "verify_reader", lambda *_args, **_kwargs: ("clinic_cloudrun_reader", 164201))
    conn = Connection()

    result = acceptance.check_reader(reader_file=Path("reader.env"), connect=lambda *_a, **_kw: conn)

    assert result == {
        "role": "clinic_cloudrun_reader", "visible_clinics": 164201,
        "visible_hp_jobs": 4, "visible_hp_job_items": 29,
    }
    assert conn.cur.sql[0] == "SET TRANSACTION READ ONLY"
    assert "FROM research.research_jobs WHERE kind='hp'" in conn.cur.sql[1]
    assert "FROM research.research_job_items i JOIN research.research_jobs j" in conn.cur.sql[2]
    assert all(query.startswith("SET TRANSACTION READ ONLY") or query.startswith("SELECT ")
               for query in conn.cur.sql)
    assert conn.rolled_back
