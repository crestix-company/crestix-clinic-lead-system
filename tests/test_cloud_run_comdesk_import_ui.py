"""Issue #130 P2-(1): source-level safety checks for cloud_comdesk_import_ui(),
following this repo's established pattern (see test_cloud_run_read_only_
isolation.py, test_simple_workflow_cleanup.py) of verifying UI-gating
structure directly from source, since exercising real Streamlit widget
interaction requires a running script context this test suite doesn't set up.

The one hard safety invariant: this panel must be completely unreachable
(not just disabled) under the current READ ONLY Canary -- the function must
return before any st.file_uploader/write call once read_only_ui_mode() is
True.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _app_source():
    return (ROOT / "app_v2.py").read_text(encoding="utf-8")


def _cloud_comdesk_import_ui_source():
    source = _app_source()
    start = source.index("def cloud_comdesk_import_ui(")
    end = source.index("\ndef ", start + 10)
    return source[start:end]


def test_returns_immediately_under_read_only_before_any_widget_or_write_call():
    block = _cloud_comdesk_import_ui_source()
    guard_pos = block.index("if read_only_ui_mode():")
    return_pos = block.index("return", guard_pos)
    body_before_return = block[:return_pos]
    # Nothing that could create a widget or touch a write path may appear
    # before the read_only_ui_mode() early return.
    for forbidden in ("st.file_uploader(", "st.button(", "clinics.import_comdesk(", "write_repositories_for("):
        assert forbidden not in body_before_return


def test_feature_flag_checked_before_any_file_uploader_or_write_call():
    block = _cloud_comdesk_import_ui_source()
    flag_pos = block.index('cloud_write_feature_enabled("CLINIC_CLOUD_COMDESK_IMPORT_ENABLED")')
    uploader_pos = block.index("st.file_uploader(")
    write_pos = block.index("clinics.import_comdesk(")
    assert flag_pos < uploader_pos < write_pos


def test_demo_mode_blocked_before_file_uploader():
    block = _cloud_comdesk_import_ui_source()
    demo_pos = block.index("if demo:")
    uploader_pos = block.index("st.file_uploader(")
    assert demo_pos < uploader_pos


def test_operator_identity_required_before_file_uploader():
    block = _cloud_comdesk_import_ui_source()
    identity_pos = block.index("operator_email_from_headers(")
    guard_pos = block.index("if not operator_email:")
    uploader_pos = block.index("st.file_uploader(")
    assert identity_pos < guard_pos < uploader_pos


def test_write_call_is_gated_by_a_confirmed_checkbox_and_submit_button():
    block = _cloud_comdesk_import_ui_source()
    confirm_pos = block.index('st.checkbox("内容を確認しました"')
    button_pos = block.index('st.button(\n            "確認のうえ取り込む"')
    write_pos = block.index("clinics.import_comdesk(")
    assert confirm_pos < button_pos < write_pos
    button_call = block[button_pos:write_pos]
    assert "disabled=not confirmed" in button_call


def test_idempotency_key_checked_before_file_uploader_result_is_trusted():
    block = _cloud_comdesk_import_ui_source()
    key_pos = block.index("comdesk_import_idempotency_key(")
    applied_check_pos = block.index('applied_key == idempotency_key')
    write_pos = block.index("clinics.import_comdesk(")
    assert key_pos < applied_check_pos < write_pos


def test_preview_runs_before_the_write_call():
    block = _cloud_comdesk_import_ui_source()
    preview_pos = block.index("preview_comdesk_import(")
    write_pos = block.index("clinics.import_comdesk(")
    assert preview_pos < write_pos


def test_operator_email_and_idempotency_key_are_logged_on_apply():
    block = _cloud_comdesk_import_ui_source()
    assert "logging.info(" in block
    log_pos = block.index("logging.info(")
    write_pos = block.index("clinics.import_comdesk(")
    assert write_pos < log_pos  # logged only after the write actually ran
    log_call = block[log_pos:block.index(")", block.index("result,", log_pos)) + 1]
    assert "operator_email" in log_call
    assert "idempotency_key" in log_call


def test_wired_into_the_existing_step1_comdesk_import_panel():
    source = _app_source()
    step_start = source.index("def import_ui_comdesk_step(")
    step_end = source.index("\ndef ", step_start + 10)
    step_body = source[step_start:step_end]
    assert "render_comdesk_snapshot_diff_import(store, demo, key_prefix=key_prefix)" in step_body
    assert "cloud_comdesk_import_ui(store, demo, key_prefix=key_prefix)" in step_body
    # Added as a sibling panel after the existing one, not replacing it.
    assert step_body.index("render_comdesk_snapshot_diff_import(") < step_body.index("cloud_comdesk_import_ui(")
