from types import SimpleNamespace


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
