"""Mirrors tests/test_cloud_run_comdesk_import_ui.py's structure exactly for
cloud_kouseikyoku_import_ui() -- every lesson from the Comdesk review (Issue
#130 PR #131) applied here from the start, not discovered after the fact."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _app_source():
    return (ROOT / "app_v2.py").read_text(encoding="utf-8")


def _cloud_kouseikyoku_import_ui_source():
    source = _app_source()
    start = source.index("def cloud_kouseikyoku_import_ui(")
    end = source.index("\ndef ", start + 10)
    return source[start:end]


def test_returns_immediately_under_read_only_or_non_cloud_run_before_any_widget_or_write_call():
    block = _cloud_kouseikyoku_import_ui_source()
    guard_pos = block.index("if read_only_ui_mode() or not is_cloud_run_deployment():")
    return_pos = block.index("return", guard_pos)
    body_before_return = block[:return_pos]
    for forbidden in ("st.file_uploader(", "st.button(", "clinics.import_master(", "write_repositories_for("):
        assert forbidden not in body_before_return


def test_feature_flag_checked_before_any_file_uploader_or_write_call():
    block = _cloud_kouseikyoku_import_ui_source()
    flag_pos = block.index('cloud_write_feature_enabled("CLINIC_CLOUD_KOUSEIKYOKU_IMPORT_ENABLED")')
    uploader_pos = block.index("st.file_uploader(")
    write_pos = block.index("clinics.import_master(")
    assert flag_pos < uploader_pos < write_pos


def test_demo_mode_blocked_before_file_uploader():
    block = _cloud_kouseikyoku_import_ui_source()
    demo_pos = block.index("if demo:")
    uploader_pos = block.index("st.file_uploader(")
    assert demo_pos < uploader_pos


def test_identity_presence_alone_is_not_treated_as_authorization():
    block = _cloud_kouseikyoku_import_ui_source()
    assert "operator_authorized_for_cloud_write(operator_email)" in block
    assert "if not operator_email:" not in block
    identity_pos = block.index("operator_email_from_headers(")
    guard_pos = block.index("if not operator_authorized_for_cloud_write(operator_email):")
    uploader_pos = block.index("st.file_uploader(")
    assert identity_pos < guard_pos < uploader_pos


def test_write_call_is_gated_by_a_confirmed_checkbox_and_submit_button():
    block = _cloud_kouseikyoku_import_ui_source()
    confirm_pos = block.index('st.checkbox("内容を確認しました"')
    button_pos = block.index('st.button(\n            "確認のうえ取り込む"')
    write_pos = block.index("clinics.import_master(")
    assert confirm_pos < button_pos < write_pos
    assert "disabled=not confirmed" in block[button_pos:write_pos]


def test_idempotency_key_checked_before_write():
    block = _cloud_kouseikyoku_import_ui_source()
    key_pos = block.index("kouseikyoku_import_idempotency_key(")
    applied_check_pos = block.index("applied_key == idempotency_key")
    write_pos = block.index("clinics.import_master(")
    assert key_pos < applied_check_pos < write_pos


def test_preview_runs_before_the_write_call():
    block = _cloud_kouseikyoku_import_ui_source()
    preview_pos = block.index("preview_kouseikyoku_import(")
    write_pos = block.index("clinics.import_master(")
    assert preview_pos < write_pos


def test_uses_dedicated_writer_connection_not_the_general_write_backend():
    block = _cloud_kouseikyoku_import_ui_source()
    assert "kouseikyoku_import_writer_repositories(" in block
    assert "write_repositories_for(store)" not in block
    writer_pos = block.index("kouseikyoku_import_writer_repositories(")
    write_pos = block.index("clinics.import_master(")
    assert writer_pos < write_pos


def test_unconfigured_writer_connection_is_handled_before_any_write_attempt():
    block = _cloud_kouseikyoku_import_ui_source()
    assert "KouseikyokuImportWriterNotConfiguredError" in block
    try_pos = block.index("try:")
    except_pos = block.index("except KouseikyokuImportWriterNotConfiguredError")
    write_pos = block.index("clinics.import_master(")
    assert try_pos < except_pos < write_pos


def test_no_misleading_never_writes_claim():
    block = _cloud_kouseikyoku_import_ui_source()
    assert "本番データは書込みません" not in block
    assert "実際に書き込みます" in block


def test_operator_email_and_idempotency_key_are_logged_on_apply():
    block = _cloud_kouseikyoku_import_ui_source()
    assert "logging.info(" in block
    log_pos = block.index("logging.info(")
    write_pos = block.index("clinics.import_master(")
    assert write_pos < log_pos
    log_call = block[log_pos:block.index(")", block.index("result,", log_pos)) + 1]
    assert "operator_email" in log_call
    assert "idempotency_key" in log_call


def test_wired_into_step2_of_simple_workflow_ui():
    source = _app_source()
    start = source.index("def simple_workflow_ui")
    end = source.index("def import_ui_comdesk_step", start)
    body = source[start:end]
    assert 'st.subheader("Step2：厚生局データを取り込む")' in body
    assert "cloud_kouseikyoku_import_ui(store, demo)" in body
    step2_pos = body.index('st.subheader("Step2：厚生局データを取り込む")')
    call_pos = body.index("cloud_kouseikyoku_import_ui(store, demo)")
    step3_pos = body.index('st.subheader("Step3：Google Maps Collector")')
    assert step2_pos < call_pos < step3_pos
