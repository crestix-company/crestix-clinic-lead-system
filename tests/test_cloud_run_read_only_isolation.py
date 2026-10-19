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
