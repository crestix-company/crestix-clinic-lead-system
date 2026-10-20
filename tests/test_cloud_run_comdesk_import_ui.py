"""Issue #130 P2-(1): source-level safety checks for cloud_comdesk_import_ui(),
following this repo's established pattern (see test_cloud_run_read_only_
isolation.py, test_simple_workflow_cleanup.py) of verifying UI-gating
structure directly from source, since exercising real Streamlit widget
interaction requires a running script context this test suite doesn't set up.

The one hard safety invariant: this panel must be completely unreachable
(not just disabled) under the current READ ONLY Canary -- the function must
return before any st.file_uploader/write call once read_only_ui_mode() is
True, and also whenever this process is not a Cloud Run deployment at all
(is_cloud_run_deployment() False, e.g. a Mac process with the flag
accidentally set).
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
    guard_pos = block.index("if read_only_ui_mode() or not is_cloud_run_deployment():")
    return_pos = block.index("return", guard_pos)
    body_before_return = block[:return_pos]
    # Nothing that could create a widget or touch a write path may appear
    # before the read_only_ui_mode()/is_cloud_run_deployment() early return.
    for forbidden in ("st.file_uploader(", "st.button(", "clinics.import_comdesk(", "write_repositories_for("):
        assert forbidden not in body_before_return


def test_non_cloud_run_process_is_also_blocked_even_if_flag_and_not_read_only():
    """Defense in depth: a Mac process that somehow has
    CLINIC_CLOUD_COMDESK_IMPORT_ENABLED set and CLINIC_READ_ONLY_UI unset
    must still never reach this panel -- is_cloud_run_deployment() (K_SERVICE,
    never present on Mac) is the second half of the guard, independent of
    both flags."""
    block = _cloud_comdesk_import_ui_source()
    assert "or not is_cloud_run_deployment()" in block
    guard_pos = block.index("if read_only_ui_mode() or not is_cloud_run_deployment():")
    uploader_pos = block.index("st.file_uploader(")
    assert guard_pos < uploader_pos


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
    guard_pos = block.index("if not operator_authorized_for_cloud_write(operator_email):")
    uploader_pos = block.index("st.file_uploader(")
    assert identity_pos < guard_pos < uploader_pos


def test_identity_presence_alone_is_not_treated_as_authorization():
    """Review fix: a non-empty IAP email used to be sufficient to proceed.
    The gate must call the allowlist-checking function, not just test
    operator_email truthiness directly."""
    block = _cloud_comdesk_import_ui_source()
    assert "operator_authorized_for_cloud_write(operator_email)" in block
    assert "if not operator_email:" not in block


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


def test_sibling_panels_render_even_when_legacy_uploader_has_no_file():
    """Review fix: `if not upload: return` used to exit the whole function
    before either sibling panel was ever reached -- an operator had to
    first pick a file for the UNRELATED legacy uploader just to make
    render_comdesk_snapshot_diff_import()/cloud_comdesk_import_ui() appear
    at all. Both calls must sit OUTSIDE (after, at the function's own
    indent level) the `if upload:` block that wraps the legacy path."""
    source = _app_source()
    step_start = source.index("def import_ui_comdesk_step(")
    step_end = source.index("\ndef ", step_start + 10)
    step_body = source[step_start:step_end]

    assert "if upload:" in step_body  # legacy path now wrapped in `if`, not early-return
    assert "if not upload:\n        return" not in step_body

    snapshot_call = '    render_comdesk_snapshot_diff_import(store, demo, key_prefix=key_prefix)\n'
    cloud_call = '    cloud_comdesk_import_ui(store, demo, key_prefix=key_prefix)\n'
    assert snapshot_call in step_body
    assert cloud_call in step_body
    # 4-space indent == function body top level, i.e. NOT nested inside the
    # `if upload:` block (which indents its contents to 8 spaces).
    assert not snapshot_call.startswith('        ')
    assert not cloud_call.startswith('        ')


def test_uses_dedicated_writer_connection_not_the_general_write_backend():
    """Review fix: the first version called write_repositories_for(store) --
    the SAME general write connection every other local write path shares.
    The actual write must go through the dedicated, fail-closed Comdesk
    import writer connection instead."""
    block = _cloud_comdesk_import_ui_source()
    assert "comdesk_import_writer_repositories(" in block
    assert "write_repositories_for(store)" not in block
    writer_pos = block.index("comdesk_import_writer_repositories(")
    write_pos = block.index("clinics.import_comdesk(")
    assert writer_pos < write_pos


def test_unconfigured_writer_connection_is_handled_before_any_write_attempt():
    block = _cloud_comdesk_import_ui_source()
    assert "ComdeskImportWriterNotConfiguredError" in block
    try_pos = block.index("try:")
    except_pos = block.index("except ComdeskImportWriterNotConfiguredError")
    write_pos = block.index("clinics.import_comdesk(")
    assert try_pos < except_pos < write_pos


def test_no_misleading_never_writes_claim_once_the_panel_is_reachable():
    """Review fix: the first version said "本番データは書込みません" even on
    the branch that, once enabled, actually calls the write path -- false
    once an operator confirms and submits. The panel must not claim writes
    never happen; it must say the operation does write to whatever DB this
    deployment is connected to."""
    block = _cloud_comdesk_import_ui_source()
    assert "本番データは書込みません" not in block
    assert "実際に書き込みます" in block


def test_session_guard_is_documented_as_ux_only_not_the_safety_boundary():
    """Review fix: duplicate-import prevention across sessions/restarts/
    multiple users comes from import_comdesk()'s own UUID-based data-layer
    idempotency, not from the in-memory st.session_state guard (which is
    lost on container restart / doesn't apply across tabs or users). The
    docstring must say so explicitly, not imply the session guard alone is
    the safety mechanism."""
    source = _app_source()
    start = source.index("def cloud_comdesk_import_ui(")
    docstring_end = source.index('"""', source.index('"""', start) + 3)
    docstring = source[start:docstring_end]
    assert "not the safety" in docstring or "not the safety boundary" in docstring


def test_nav_lock_makes_email_and_settings_pages_structurally_unreachable_on_cloud_run():
    """Review point 2: "既存の取込・HP Job・Email・設定画面が意図せず開放され
    ないことを保証する." main() routes to Email調査/メール送付文章/詳細設定
    purely via `if nav == "...": ...` against the value the st.radio widget
    returns -- and that widget can only ever return one of nav_options. Once
    nav_locked makes nav_options == NAV[:2], those three branches become
    structurally unreachable (not just hidden-but-reachable-by-URL), because
    there is no other code path in main() that sets `nav` to those values."""
    source = _app_source()
    main_start = source.index("def main():")
    main_body = source[main_start:source.index("\ndef ", main_start + 10)]
    assert 'if nav == "かんたん操作":' in main_body
    assert 'elif nav == "Email調査":' in main_body
    assert 'elif nav == "メール送付文章":' in main_body
    # nav itself only ever comes from st.radio("メニュー", nav_options, ...);
    # confirm there is no second assignment to `nav` that could bypass nav_options.
    assert main_body.count("nav = st.radio(") == 1
    assert "nav = \"" not in main_body  # no hardcoded override anywhere else
