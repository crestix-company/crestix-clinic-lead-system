from types import SimpleNamespace


def test_read_only_ui_connection_sets_server_enforced_transaction_guard(monkeypatch):
    from src.repository.supabase_adapter import connect

    captured = {}

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def execute(self, statement): captured.setdefault("statements", []).append(statement)

    class Connection:
        autocommit = False
        def cursor(self): return Cursor()

    def fake_connect(url, **kwargs):
        captured.update(url=url, kwargs=kwargs)
        return Connection()

    monkeypatch.setenv("CLINIC_READ_ONLY_UI", "true")
    monkeypatch.setattr("psycopg.connect", fake_connect)

    connect("postgresql://example.invalid/postgres", autocommit=True)

    assert "default_transaction_read_only=on" in captured["kwargs"]["options"]
    assert captured["kwargs"]["connect_timeout"] == 20
    assert captured["statements"] == [
        "SET extra_float_digits = 3",
        "SET default_transaction_read_only = on",
        "SET statement_timeout = 60000",
    ]


def test_production_store_factory_never_constructs_sqlite(monkeypatch):
    import app_v2
    sentinel = object()
    monkeypatch.setenv("CLINIC_DATA_BACKEND", "supabase")
    monkeypatch.setattr("src.repository.runtime_store.SupabaseRuntimeStore", lambda: sentinel)
    monkeypatch.setattr(
        app_v2, "ClinicStore",
        lambda *_: (_ for _ in ()).throw(AssertionError("SQLite opened")),
    )
    app_v2.store_for.clear()
    assert app_v2.store_for("/definitely/not/exist/clinic.sqlite3") is sentinel


def test_supabase_runtime_ignores_all_sqlite_path_environment(monkeypatch):
    import app_v2
    sentinel = object()
    monkeypatch.setenv("CLINIC_DATA_BACKEND", "supabase")
    for key in ("CLINIC_DB_PATH", "TREATMENT_RESEARCH_DB_PATH", "HP_RESEARCH_BATCH_DB_PATH"):
        monkeypatch.setenv(key, "/definitely/not/exist/" + key + ".sqlite3")
    monkeypatch.setattr("src.repository.runtime_store.SupabaseRuntimeStore", lambda: sentinel)
    monkeypatch.setattr(
        app_v2, "ClinicStore",
        lambda *_: (_ for _ in ()).throw(AssertionError("SQLite opened")),
    )
    app_v2.store_for.clear()
    assert app_v2.store_for(None) is sentinel


def test_email_status_metrics_read_only_uses_projection_and_only_aggregates_status():
    from src.repository.runtime_store import SupabaseRuntimeStore

    class Cursor:
        def __init__(self):
            self.statement = ""
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def execute(self, statement): self.statement = statement
        def fetchall(self):
            return [("FOUND", 12), ("NOT_RESEARCHED", 30)]

    class Connection:
        def __init__(self): self.cursor_value = Cursor()
        def cursor(self): return self.cursor_value

    store = object.__new__(SupabaseRuntimeStore)
    store._conn = Connection()
    assert store.email_research_status_metrics() == {"FOUND": 12, "NOT_RESEARCHED": 30}
    sql = store._conn.cursor_value.statement
    assert "clinic_email_status_readonly" in sql
    assert "research_status,count(*)" in sql
    assert "email_candidates" not in sql


def test_supabase_job_runner_uses_database_claim_not_local_file_lock(monkeypatch):
    import src.master.jobs as jobs
    store = SimpleNamespace(is_supabase_runtime=True)
    called = []
    monkeypatch.setattr(jobs, "_run_locked", lambda *args: called.append(args))
    monkeypatch.setattr(
        jobs, "FileLock", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("local lock opened")),
    )
    assert jobs.run_job(store, "job", object()) is True
    assert called and called[0][0] is store


def test_production_launchers_contain_no_sqlite_path_contract():
    from src.utils.config import ROOT
    paths = (
        ROOT / "scripts/launch_v2_mac.sh",
        ROOT / "scripts/launch_v2_windows.ps1",
        ROOT / "scripts/update_and_launch_windows.ps1",
    )
    forbidden = ("CLINIC_DB_PATH", "TREATMENT_RESEARCH_DB_PATH", "HP_RESEARCH_BATCH_DB_PATH")
    for path in paths:
        text = path.read_text(encoding="utf-8-sig")
        assert all(name not in text for name in forbidden)


def test_external_acceptance_markers_are_stable_distinct_integers():
    from scripts.supabase_migration.stage5_external_acceptance import _marker, _validate_token
    token = "__stage5_multipc_acceptance_unit-12345678"
    _validate_token(token, "multipc")
    first = _marker(token, "a_to_b")
    reverse = _marker(token, "b_to_a")
    assert isinstance(first, int) and 1_500_000_000 <= first < 2_000_000_000
    assert first == _marker(token, "a_to_b")
    assert first != reverse


class _RuntimeConn:
    def __init__(self, *, closed=False, broken=False):
        self.closed = closed
        self.broken = broken

    def close(self):
        self.closed = True


def _runtime_repositories(conn, metrics_fn):
    clinics = SimpleNamespace(_conn=conn, metrics=metrics_fn)
    return SimpleNamespace(
        clinics=clinics,
        hp_research=SimpleNamespace(),
        treatment=SimpleNamespace(),
        research=SimpleNamespace(),
        provenance=SimpleNamespace(),
        settings=SimpleNamespace(),
    )


def test_supabase_runtime_rebuilds_cached_read_connection_when_already_closed(monkeypatch):
    from src.repository.runtime_store import SupabaseRuntimeStore

    first = _runtime_repositories(
        _RuntimeConn(closed=True),
        lambda: (_ for _ in ()).throw(AssertionError("closed repository must not be used")),
    )
    second = _runtime_repositories(_RuntimeConn(), lambda: {"全マスター": 123})
    bundles = iter([first, second])
    monkeypatch.setattr(
        "src.repository.backend.build_repositories",
        lambda *_args, **_kwargs: next(bundles),
    )

    store = SupabaseRuntimeStore()

    assert store.metrics() == {"全マスター": 123}
    assert store.repositories is second


def test_supabase_runtime_retries_one_read_after_operational_disconnect(monkeypatch):
    import psycopg
    from src.repository.runtime_store import SupabaseRuntimeStore

    first_conn = _RuntimeConn()
    first = _runtime_repositories(
        first_conn,
        lambda: (_ for _ in ()).throw(psycopg.OperationalError("the connection is closed")),
    )
    second = _runtime_repositories(_RuntimeConn(), lambda: {"全マスター": 456})
    bundles = iter([first, second])
    monkeypatch.setattr(
        "src.repository.backend.build_repositories",
        lambda *_args, **_kwargs: next(bundles),
    )

    store = SupabaseRuntimeStore()

    assert store.metrics() == {"全マスター": 456}
    # Don't close a connection that another Streamlit fragment may still be using.
    assert first_conn.closed is False
    assert store.repositories is second
