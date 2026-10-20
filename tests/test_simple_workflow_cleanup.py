from pathlib import Path

import app_v2
from streamlit.testing.v1 import AppTest


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


def test_single_job_status_is_hidden_from_simple_workflow_and_managed_in_advanced_settings():
    source = _app_source()
    simple_start = source.index("def simple_workflow_ui")
    simple_end = source.index("def simple_sales_ui", simple_start)
    simple = source[simple_start:simple_end]
    admin_start = source.index("def single_hp_job_recovery_ui")
    admin_end = source.index("def advanced_ui", admin_start)
    admin = source[admin_start:admin_end]

    assert "現在の調査" not in simple
    assert "simple_pause" not in simple and "simple_resume" not in simple and "simple_reset" not in simple
    assert '"HP単発Job管理・復旧"' in source
    assert '"advanced_single_hp_pause"' in admin
    assert '"advanced_single_hp_resume"' in admin
    assert '"advanced_single_hp_reset"' in admin
    assert "recent_jobs(store)" in admin and "job_status(store, selected)" in admin
    assert "show_job_progress(store, current[\"id\"])" in admin


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


def test_step6_breakdown_matches_sync_exclusion_order_and_labels_populations():
    class FixtureStore:
        is_supabase_runtime = False

        def count(self, filters):
            return 5

        def query(self, filters, *, limit, offset):
            return [
                {"id": 1, "uuid": "", "merge_hold": 0},
                {"id": 2, "uuid": "", "merge_hold": 0},
                {"id": 3, "uuid": "", "merge_hold": 1},
                {"id": 4, "uuid": "", "merge_hold": 0},
                {"id": 5, "uuid": "UUID-5", "merge_hold": 0},
            ]

        def comdesk_linkage_state(self):
            return {"imported_clinic_ids": {1}, "held_clinic_ids": {2}}

        def web_research_metrics(self):
            return {"url_acquired": 20, "researched": 12}

    rows = {row["項目"]: row for row in app_v2.comdesk_sync_breakdown(
        FixtureStore(), {"厚生局取込レコード": 50},
    )}
    assert rows["厚生局全件"]["件数"] == "50件"
    assert rows["HP URL取得済み"]["件数"] == "20件"
    assert rows["HP調査完了"]["件数"] == "12件"
    assert rows["UUID未付与"]["件数"] == "4件"
    exclusions = sum(int(rows[key]["件数"].removesuffix("件")) for key in (
        "除外：Comdesk取込済み", "除外：照合保留", "除外：merge hold",
        "最終Comdesk同期対象",
    ))
    assert exclusions == 4
    assert "hp_url" in rows["HP URL取得済み"]["母集団・条件"]
    assert "最新完了HP Job" in rows["HP成功医院（同期候補の母集団）"]["母集団・条件"]


def test_step6_breakdown_fails_closed_when_hp_population_is_unknown():
    class FixtureStore:
        is_supabase_runtime = False

        def count(self, filters):
            return 0

        def query(self, filters, *, limit, offset):
            return []

        def comdesk_linkage_state(self):
            return {"imported_clinic_ids": set(), "held_clinic_ids": set()}

        def web_research_metrics(self):
            return None

    import pytest
    with pytest.raises(ValueError, match="HP URL母集団"):
        app_v2.comdesk_sync_breakdown(FixtureStore(), {})


def test_single_hp_job_controls_survive_rerun_in_advanced_settings():
    app = AppTest.from_string(r'''
import streamlit as st
import app_v2

st.session_state.setdefault("job_fixture", {
    "id": "fixture-hp-job", "kind": "hp", "status": "RUNNING",
    "created_at": "2026-10-20T10:00:00", "options_json": {},
    "total": 2, "counts": {"PENDING": 2, "RUNNING": 0, "DONE": 0},
    "results": {},
})
job = st.session_state["job_fixture"]

class FakeRunner:
    def running(self):
        return False
    def start(self, store, jid, provider):
        job["status"] = "RUNNING"

class FakeJobs:
    def pause_job(self, jid):
        job["status"] = "PAUSED"
    def reset_job(self, jid):
        job["status"] = "PENDING"

class FakeRepositories:
    jobs = FakeJobs()

class FakeStore:
    path = "fixture.db"

app_v2.recent_jobs = lambda store: [job]
app_v2.job_status = lambda store, jid: job
app_v2.runner_for = lambda path: FakeRunner()
app_v2.hp_executor_mode = lambda: "local"
app_v2.write_repositories_for = lambda store: FakeRepositories()
app_v2.show_job_progress = lambda store, jid: st.caption("保存状態：" + job["status"])
app_v2.single_hp_job_recovery_ui(FakeStore(), demo=False)
''').run()
    assert not app.exception
    assert any("保存状態：RUNNING" in item.value for item in app.caption)

    next(button for button in app.button if button.label == "一時停止").click().run()
    assert not app.exception
    assert any("保存状態：PAUSED" in item.value for item in app.caption)
    assert any(not button.disabled for button in app.button if button.label == "この調査を再開")

    next(button for button in app.button if button.label == "この調査を再開").click().run()
    assert not app.exception
    assert any("保存状態：RUNNING" in item.value for item in app.caption)


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

    assert "nav_options = NAV[:2] if nav_locked else NAV" in main
    assert "nav_locked = read_only or is_cloud_run_deployment()" in main
    assert "if not read_only and not is_cloud_run_executor() and not is_cloud_run_deployment():" in main
    assert "write_repositories_for(store).clinics.refresh_age_model()" in main
    assert "not read_only and not is_cloud_run_deployment() and auto_state.get" in simple
    assert "write_disabled = demo or read_only" in simple
    assert "READ ONLY画面確認中" in simple


def test_cloud_run_deployment_blocks_human_review_cards_and_email_auto_card():
    """Issue #130 review point 2: hp_human_review_card/email_human_review_card
    write via their own write_repositories_for(store) calls from internal
    button handlers with no is_cloud_run_deployment() awareness at all (they
    predate the Cloud Run write-path work). A CLINIC_READ_ONLY_UI=false Cloud
    Run Web deployment must not render them at all -- gated at this one call
    site, consistent with nav_locked/write_disabled elsewhere in this file."""
    source = _app_source()
    simple_start = source.index("def simple_workflow_ui")
    simple_end = source.index("def import_ui_comdesk_step", simple_start)
    simple = source[simple_start:simple_end]

    assert "elif is_cloud_run_deployment():" in simple
    cloud_block_pos = simple.index("elif is_cloud_run_deployment():")
    cards_pos = simple.index("hp_human_review_card(store)")
    assert cloud_block_pos < cards_pos
    # The is_cloud_run_deployment() branch must render instead of the cards,
    # not alongside them (else/elif, not two independent ifs).
    between = simple[cloud_block_pos:cards_pos]
    assert "else:" in between
    assert "hp_human_review_card(store)" not in between


def test_cloud_run_deployment_blocks_human_review_sidebar_and_dialogs_in_main():
    """Same concern as the card gating above, for main()'s sidebar/dialog
    equivalents (hp_human_review_sidebar, email_human_review_sidebar,
    hp_human_review_dialog, email_human_review_dialog) -- these also write
    via write_repositories_for(store) from their own button handlers."""
    source = _app_source()
    main_start = source.index("def main():")
    main_body = source[main_start:source.index("\ndef ", main_start + 10)]
    assert "if not read_only and not is_cloud_run_deployment():" in main_body
    guard_pos = main_body.index("if not read_only and not is_cloud_run_deployment():")
    sidebar_pos = main_body.index("hp_human_review_sidebar(store)")
    dialog_pos = main_body.index("hp_human_review_dialog(store)")
    assert guard_pos < sidebar_pos < dialog_pos
