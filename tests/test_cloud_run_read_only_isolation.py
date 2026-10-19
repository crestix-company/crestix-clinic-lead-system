"""Priority item 3: READ ONLY画面（CLINIC_READ_ONLY_UI=true）とCloud Run HP Job制御
（CLINIC_HP_EXECUTOR=cloud_run、専用job-controlロール）が確実に分離されていることの検証。

READ ONLY画面はSELECT専用ロールで動くことが前提（Phase A）であり、Job制御専用ロール
（SUPABASE_JOB_CONTROL_DB_URL）を必要とする操作が万一READ ONLY画面から到達可能だと、
「READ ONLYなのに書込ロール資格情報を要求する」という設計矛盾になる。write_disabled
（demo or read_only）がCloud Run分岐を含むボタン全体を覆っていることをソース検証する。
"""
from pathlib import Path

import pytest


def _app_source():
    return Path("app_v2.py").read_text(encoding="utf-8")


def _simple_workflow_ui_source():
    source = _app_source()
    start = source.index("def simple_workflow_ui")
    end = source.index("def simple_sales_ui", start)
    return source[start:end]


def test_hp_start_button_disabled_includes_write_disabled():
    simple = _simple_workflow_ui_source()
    start_button = simple.index('key="simple_start"')
    button_call_start = simple.rindex("if st.button(", 0, start_button)
    button_call = simple[button_call_start:start_button]
    assert "write_disabled" in button_call


def test_cloud_run_dispatch_branch_is_nested_inside_the_start_button_block():
    """The `if cloud_run_executor:` branch that calls _create_cloud_hp_job()/
    _dispatch_cloud_hp_job() must be inside the same `if st.button(...)` block
    gated by write_disabled -- never reachable on a plain page render."""
    simple = _simple_workflow_ui_source()
    start_button = simple.index('key="simple_start"')
    next_button = simple.index("st.button(", simple.index("):", start_button) + 1)
    block = simple[start_button:next_button]
    assert "if cloud_run_executor:" in block
    assert "_create_cloud_hp_job(" in block
    assert "_dispatch_cloud_hp_job(" in block


def test_pause_button_disabled_includes_write_disabled_and_routes_cloud_run_pause():
    simple = _simple_workflow_ui_source()
    pause_button = simple.index('key="simple_pause"')
    can_pause_start = simple.rindex("can_pause = ", 0, pause_button)
    can_pause_block = simple[can_pause_start:pause_button]
    assert "write_disabled" in can_pause_block
    next_button = simple.index("controls[1].button(", pause_button)
    block = simple[pause_button:next_button]
    assert "_pause_cloud_hp_job(" in block


def test_resume_button_disabled_includes_write_disabled_and_routes_cloud_run_dispatch():
    simple = _simple_workflow_ui_source()
    resume_button = simple.index('key="simple_resume"')
    can_resume_start = simple.rindex("can_resume = (", 0, resume_button)
    can_resume_block = simple[can_resume_start:resume_button]
    assert "write_disabled" in can_resume_block
    next_section = simple.index("can_reset = ", resume_button)
    block = simple[resume_button:next_section]
    assert "_dispatch_cloud_hp_job(" in block


def test_read_only_warning_banner_still_present():
    simple = _simple_workflow_ui_source()
    assert "READ ONLY画面確認中のため、HP・Email JobおよびHuman Review操作は無効です。" in simple


def test_job_control_module_is_never_imported_at_app_v2_module_level():
    """src.cloud_run.job_control (and its SUPABASE_JOB_CONTROL_DB_URL
    requirement) must only ever be imported lazily inside the cloud_run-only
    helper functions, never at app_v2.py's top level -- otherwise simply
    importing app_v2 (e.g. the READ ONLY Web image) would require that env
    var to exist."""
    import ast

    tree = ast.parse(_app_source())
    module_level_names = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            module_level_names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module_level_names.append(node.module or "")
    assert not any("job_control" in name for name in module_level_names)


def test_importing_app_v2_never_requires_job_control_db_url(monkeypatch):
    monkeypatch.delenv("SUPABASE_JOB_CONTROL_DB_URL", raising=False)
    import importlib
    import app_v2
    importlib.reload(app_v2)  # must not raise JobControlNotConfiguredError


def test_read_only_workflow_uses_clinic_projection_only(monkeypatch):
    import app_v2

    class Store:
        def __init__(self):
            self.calls = []

        def count(self, filters):
            self.calls.append(("count", filters))
            return 2

        def query(self, filters, *, limit):
            self.calls.append(("query", filters, limit))
            return [{"id": 1, "clinic_name": "A Clinic"}, {"id": 2, "clinic_name": "B Clinic"}]

    class StreamlitStub:
        def __getattr__(self, name):
            return lambda *args, **kwargs: None

    store = Store()
    monkeypatch.setattr(app_v2, "st", StreamlitStub())
    app_v2.simple_workflow_ui(store, demo=False, read_only=True)

    assert [call[0] for call in store.calls] == ["count", "query"]
    assert store.calls[0][1].scope == app_v2.SCOPE_ALL
    assert store.calls[0][1].active_only is False


def test_read_only_sales_export_avoids_revision_and_uses_narrow_export():
    source = _app_source()
    start = source.index("def simple_sales_ui")
    end = source.index("def advanced_ui", start)
    sales = source[start:end]
    assert '"read_only" if read_only else store.revision()' in sales
    assert "store.export_read_only(sales_filters) if read_only else store.export(sales_filters)" in sales
    listing_start = source.index("def listing(")
    listing_end = source.index("def upload_table", listing_start)
    listing = source[listing_start:listing_end]
    assert "if not read_only:" in listing
    assert "email_repo.status_for_ids" in listing


def test_read_only_export_contract_does_not_read_provenance_or_email():
    from types import SimpleNamespace

    from src.master.comdesk import COMDESK_EXPORT_HEADERS
    from src.master.filters import Filters
    from src.repository.runtime_store import SupabaseRuntimeStore

    record = {
        "id": 7,
        "clinic_name": "Sample Clinic",
        "phone": "03-1234-5678",
        "prefecture": "東京都",
        "address": "東京都千代田区1-2-3",
        "effective_hp_rank": "A",
    }

    class Clinics:
        _conn = object()

        def __init__(self):
            self.queries = []

        def count(self, filters, as_of=None):
            return 1

        def query(self, filters, limit, offset=0, as_of=None):
            self.queries.append((filters, limit, offset, as_of))
            return [record]

    clinics = Clinics()
    store = SupabaseRuntimeStore(repositories=SimpleNamespace(clinics=clinics))
    filters = Filters(active_only=False, hp_only=False)
    count = store.count(filters)
    files = store.export_read_only(filters)
    rows = files["final_comdesk_import.csv"].decode("utf-8-sig").splitlines()

    assert list(__import__("csv").reader([rows[0]]))[0] == COMDESK_EXPORT_HEADERS
    assert len(rows) - 1 == count == 1
    assert len(list(__import__("csv").reader([rows[1]]))[0]) == len(COMDESK_EXPORT_HEADERS)
    assert list(__import__("csv").reader([rows[1]]))[0][-2:] == ["A", ""]
    assert len(clinics.queries) == 1
    assert clinics.queries[0][0] == filters
