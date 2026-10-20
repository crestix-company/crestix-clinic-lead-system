from pathlib import Path

import app_v2


def _app_source():
    return Path("app_v2.py").read_text(encoding="utf-8")


def test_simple_workflow_no_longer_contains_duplicate_comdesk_steps():
    source = _app_source()
    start = source.index("def simple_workflow_ui")
    end = source.index("def simple_sales_ui", start)
    simple = source[start:end]

    assert 'st.subheader("5. Comdesk形式で出力")' not in simple
    assert 'st.subheader("6. 5.で出したデータを手動でComdeskに入れる（＝UUIDを付与）")' not in simple
    assert 'st.subheader("7. 6.でUUIDが付与されたデータをこのアプリに取り込む")' not in simple
    assert 'key="simple_comdesk_upload"' not in simple
    assert 'key_prefix="simple_step5"' not in simple


def test_comdesk_export_and_import_remain_available_in_dedicated_pages():
    source = _app_source()

    sales_start = source.index("def simple_sales_ui")
    sales_end = source.index("def advanced_ui", sales_start)
    sales = source[sales_start:sales_end]
    assert 'st.subheader("営業対象をComdesk形式で出力")' in sales
    assert 'key="simple_sales_filtered_export"' in sales
    assert "comdesk_sync_candidates" not in sales
    assert "import_ui_comdesk_step" not in sales
    assert 'listing(store, sales_filters, "simple_sales_results", read_only=read_only)' in sales

    import_start = source.index("def import_ui")
    import_end = source.index("def ", import_start + 10)
    import_block = source[import_start:import_end]
    assert 'st.subheader("1. 既存コムデスクを取り込む")' in import_block
    assert 'key="comdesk_upload"' in import_block


def test_simple_workflow_keeps_step1_through_step7_in_order():
    source = _app_source()
    start = source.index("def simple_workflow_ui")
    end = source.index("def simple_sales_ui", start)
    simple = source[start:end]
    headings = [
        "Step1：既存Comdesk CSV・Excelを取り込む",
        "Step2：厚生局データを取り込む",
        "Step3：Google Maps Collector",
        "Step4：Google Maps結果を取り込む",
        "Step5：HP・Email自動調査",
        "Step6：HP確認済み医院をComdesk同期用CSVで出力",
        "Step7：UUID付与後のComdesk CSVを再取り込む",
    ]

    positions = [simple.index(heading) for heading in headings]
    assert positions == sorted(positions)
    assert "render_comdesk_sync_export(store, demo)" in simple
    assert 'key_prefix="simple_comdesk_reimport"' in simple


def test_simple_sync_and_sales_export_use_separate_conditions_and_actions():
    source = _app_source()
    sync_start = source.index("def comdesk_sync_candidates")
    sync_end = source.index("def simple_sales_ui", sync_start)
    sync = source[sync_start:sync_end]
    sales_start = source.index("def simple_sales_ui")
    sales_end = source.index("def advanced_ui", sales_start)
    sales = source[sales_start:sales_end]

    assert "hp_job_export_summary" in sync
    assert "hp_only=True" in sync
    assert "effective_ranks" not in sync
    assert "comdesk_linkage_state" in sync
    assert "simple_comdesk_sync_export" not in sales
    assert "simple_comdesk_reimport" not in sales
    assert "sales_filters" in sales


def test_read_only_ui_mode_is_explicit_and_fail_closed(monkeypatch):
    monkeypatch.delenv("CLINIC_READ_ONLY_UI", raising=False)
    assert app_v2.read_only_ui_mode() is False
    monkeypatch.setenv("CLINIC_READ_ONLY_UI", "true")
    assert app_v2.read_only_ui_mode() is True


def test_read_only_ui_skips_startup_writes_and_auto_resume():
    source = _app_source()
    main_start = source.index("def main()")
    main = source[main_start:]
    simple_start = source.index("def simple_workflow_ui")
    simple_end = source.index("def import_ui_comdesk_step", simple_start)
    simple = source[simple_start:simple_end]

    assert "nav_options = NAV[:2] if read_only else NAV" in main
    assert "if not read_only and not is_cloud_run_executor():" in main
    assert "write_repositories_for(store).clinics.refresh_age_model()" in main
    assert "if not read_only and auto_state.get" in simple
    assert "write_disabled = demo or read_only" in simple
    assert "READ ONLY画面確認中" in simple


def test_hp_research_requires_explicit_prefecture_and_medical_type():
    source = _app_source()
    start = source.index("def simple_workflow_ui")
    end = source.index("def import_ui_comdesk_step", start)
    simple = source[start:end]

    assert 'PREF_PLACEHOLDER = "未選択（選択してください）"' in simple
    assert 'pref_options = [PREF_PLACEHOLDER, "すべて"] + prefs' in simple
    assert 'MEDICAL_TYPE_PLACEHOLDER = "未選択"' in simple
    assert 'index=0, key="simple_research_pref"' in simple
    assert 'index=0, key="simple_research_medical_type"' in simple
    assert "area_and_medical_type_selected = pref_explicitly_selected and medical_type_explicitly_selected" in simple
    assert "or not area_and_medical_type_selected" in simple
    assert simple.count("or not area_and_medical_type_selected") == 2
    assert "research_medical_types = []" in simple


def test_sales_unselected_filters_and_default_rank_are_preserved():
    source = _app_source()
    sales_start = source.index("def simple_sales_ui")
    sales_end = source.index("def advanced_ui", sales_start)
    sales = source[sales_start:sales_end]

    assert 'SALES_MEDICAL_TYPE_PLACEHOLDER = "未選択（絞り込みなし）"' in sales
    assert 'index=0, key="simple_sales_medical_type"' in sales
    assert '[] if med_label == SALES_MEDICAL_TYPE_PLACEHOLDER else [med_label]' in sales
    assert 'st.multiselect("都道府県", prefs, default=[], key="simple_sales_pref")' in sales
    assert 'rank_choice = cols[2].selectbox("HP ABC判定", rank_labels, index=default_rank_index' in sales
    assert "HP_RANK_PRESET_DEFAULT_INDEX = next(i for i, (label, _) in enumerate(HP_RANK_PRESETS) if label == \"A+B\")" in source
    assert 'export_ranks = [r for r in (hp_ranks or ["A", "B"]) if r in ("A", "B")]' in sales


def test_cloud_run_phase_c_and_read_only_controls_remain_gated():
    source = _app_source()
    start = source.index("def simple_workflow_ui")
    end = source.index("def import_ui_comdesk_step", start)
    simple = source[start:end]

    assert "_dispatch_cloud_hp_job(jid, clinic_count=actual)" in simple
    assert 'disabled=write_disabled or auto_state.get("status") not in {"RUNNING", "WAITING_NETWORK"}' in simple
    assert '"自動調査を再開", disabled=write_disabled or auto_state.get("status") not in {"PAUSED", "ERROR"}' in simple
    assert "write_disabled or runner.running() or unfinished or auto_active or actual == 0" in simple
    assert "or not area_and_medical_type_selected" in simple
