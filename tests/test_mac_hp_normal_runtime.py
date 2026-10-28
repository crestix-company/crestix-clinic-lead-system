import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "launch_v2_mac_normal", ROOT / "scripts/launch_v2_mac_normal.py"
)
normal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(normal)


def test_normal_runtime_keeps_reader_and_writer_connections_distinct(monkeypatch, tmp_path):
    env = {"SUPABASE_RUNTIME_DB_URL": "postgresql://writer/private"}
    reader_paths = []
    preflight = []
    monkeypatch.setattr(normal, "os", type("OS", (), {"environ": env})())
    monkeypatch.setattr("scripts.launch_v2.load_runtime_env", lambda _root: True)
    monkeypatch.setattr(normal, "load_reader_dsn", lambda path=None: (reader_paths.append(path) or "postgresql://reader/private"))
    monkeypatch.setattr(normal, "verify_reader", lambda _dsn, **kw: (preflight.append(kw) or ("clinic_cloudrun_reader", 123)))

    role, count = normal.configure_normal_runtime(tmp_path)

    assert (role, count) == ("clinic_cloudrun_reader", 123)
    assert env["SUPABASE_READONLY_DB_URL"].startswith("postgresql://reader/")
    assert env["SUPABASE_RUNTIME_DB_URL"].startswith("postgresql://writer/")
    assert env["CLINIC_NORMAL_MAC_RUNTIME"] == "1"
    assert "TAVILY_API_KEY" not in env
    assert reader_paths == [normal.NORMAL_READER_FILE]
    assert preflight[0]["required_tables"] == normal.NORMAL_HP_REQUIRED_TABLES
    assert preflight[0]["required_columns"] == normal.NORMAL_HP_REQUIRED_COLUMNS


def test_normal_runtime_rejects_same_reader_and_writer_url(monkeypatch, tmp_path):
    env = {"SUPABASE_RUNTIME_DB_URL": "postgresql://same/private"}
    monkeypatch.setattr(normal, "os", type("OS", (), {"environ": env})())
    monkeypatch.setattr("scripts.launch_v2.load_runtime_env", lambda _root: True)
    monkeypatch.setattr(normal, "load_reader_dsn", lambda _path=None: "postgresql://same/private")

    with pytest.raises(RuntimeError, match="同一"):
        normal.configure_normal_runtime(tmp_path)


def test_hp_reader_contract_excludes_email_and_source_relations():
    from scripts.launch_v2_mac_reader import NORMAL_HP_REQUIRED_COLUMNS, NORMAL_HP_REQUIRED_TABLES

    assert set(NORMAL_HP_REQUIRED_TABLES) == {
        "public.clinic_cloudrun_readonly",
        "hp_research.clinic_hp_research",
        "research.research_jobs",
        "research.research_job_items",
    }
    assert all("email" not in relation for relation in NORMAL_HP_REQUIRED_TABLES)
    assert "public.clinics" not in NORMAL_HP_REQUIRED_TABLES
    assert {"phone", "departments_json"} <= set(NORMAL_HP_REQUIRED_COLUMNS["public.clinic_cloudrun_readonly"])
    assert NORMAL_HP_REQUIRED_COLUMNS["hp_research.clinic_hp_research"] == (
        "clinic_id", "fetch_status", "hp_url", "final_url", "machine_hp_rank",
    )


def test_supabase_read_factory_uses_readonly_url_and_readonly_session(monkeypatch):
    import src.repository.supabase_adapter as adapter
    from src.repository.backend import build_repositories

    calls = []

    class Connection:
        pass

    monkeypatch.setenv("CLINIC_NORMAL_MAC_RUNTIME", "1")
    monkeypatch.setenv("SUPABASE_READONLY_DB_URL", "postgresql://reader/private")
    monkeypatch.setenv("SUPABASE_RUNTIME_DB_URL", "postgresql://writer/private")
    monkeypatch.setattr(adapter, "connect", lambda *args, **kwargs: calls.append((args, kwargs)) or Connection())

    build_repositories("supabase")

    assert calls == [(("postgresql://reader/private",), {"autocommit": True, "read_only": True})]


def test_read_only_runtime_never_falls_back_to_writer_url(monkeypatch):
    from src.repository.backend import build_repositories

    monkeypatch.setenv("CLINIC_READ_ONLY_UI", "1")
    monkeypatch.delenv("SUPABASE_READONLY_DB_URL", raising=False)
    monkeypatch.setenv("SUPABASE_RUNTIME_DB_URL", "postgresql://writer/private")

    with pytest.raises(RuntimeError, match="SUPABASE_READONLY_DB_URL"):
        build_repositories("supabase")


def test_ui_render_does_not_start_hp_worker_implicitly():
    source = (ROOT / "app_v2.py").read_text(encoding="utf-8")
    simple = source[source.index("def simple_workflow_ui"):source.index("def simple_sales_ui")]
    assert '"HP調査Jobを作成"' in simple
    assert '"このHP JobのWorkerを開始・再開"' in simple
    assert '"Auto Run Workerを開始・再開"' in simple
    auto_worker_control = simple.index('"Auto Run Workerを開始・再開"')
    assert "auto_runner.start(store)" not in simple[:auto_worker_control]
    assert "auto_runner.start(store)" in simple[auto_worker_control:]
    assert '"このHP Jobを一時停止"' in simple
