"""Mirrors tests/test_cloud_run_comdesk_import_ui.py's structure for
cloud_maps_import_ui() -- every lesson from the Comdesk review (Issue #130
PR #131, including its follow-up review) applied here from the start."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _app_source():
    return (ROOT / "app_v2.py").read_text(encoding="utf-8")


def _cloud_maps_import_ui_source():
    source = _app_source()
    start = source.index("def cloud_maps_import_ui(")
    end = source.index("\ndef ", start + 10)
    return source[start:end]


def test_returns_immediately_under_read_only_before_any_widget_or_write_call():
    block = _cloud_maps_import_ui_source()
    guard_pos = block.index("if read_only_ui_mode() or not is_cloud_run_deployment():")
    return_pos = block.index("return", guard_pos)
    body_before_return = block[:return_pos]
    for forbidden in ("st.file_uploader(", "st.button(", "writer_repositories.provenance.import_maps_results(", "write_repositories_for("):
        assert forbidden not in body_before_return


def test_non_cloud_run_process_is_also_blocked_even_if_flag_and_not_read_only():
    block = _cloud_maps_import_ui_source()
    assert "or not is_cloud_run_deployment()" in block
    guard_pos = block.index("if read_only_ui_mode() or not is_cloud_run_deployment():")
    uploader_pos = block.index("st.file_uploader(")
    assert guard_pos < uploader_pos


def test_feature_flag_checked_before_any_file_uploader_or_write_call():
    block = _cloud_maps_import_ui_source()
    flag_pos = block.index('cloud_write_feature_enabled("CLINIC_CLOUD_MAPS_IMPORT_ENABLED")')
    uploader_pos = block.index("st.file_uploader(")
    write_pos = block.index("writer_repositories.provenance.import_maps_results(")
    assert flag_pos < uploader_pos < write_pos


def test_does_not_take_a_demo_or_write_disabled_parameter():
    """Issue #130 PR #131 follow-up review lesson applied here: an earlier
    draft of the sibling Comdesk panel received the caller's write_disabled
    through a parameter named `demo`, permanently blocking it on Cloud Run.
    This panel's own is_cloud_run_deployment()/flag/operator checks are its
    complete, independent gate -- it must not take that parameter at all."""
    source = _app_source()
    start = source.index("def cloud_maps_import_ui(")
    signature_end = source.index(")", start)
    signature = source[start:signature_end]
    assert "demo" not in signature
    assert "write_disabled" not in signature
    block = _cloud_maps_import_ui_source()
    assert "if demo:" not in block


def test_operator_identity_required_before_file_uploader():
    block = _cloud_maps_import_ui_source()
    identity_pos = block.index("operator_email_from_headers(")
    guard_pos = block.index("if not operator_authorized_for_cloud_write(operator_email):")
    uploader_pos = block.index("st.file_uploader(")
    assert identity_pos < guard_pos < uploader_pos


def test_identity_presence_alone_is_not_treated_as_authorization():
    block = _cloud_maps_import_ui_source()
    assert "operator_authorized_for_cloud_write(operator_email)" in block
    assert "if not operator_email:" not in block


def test_write_call_is_gated_by_a_confirmed_checkbox_and_submit_button():
    block = _cloud_maps_import_ui_source()
    confirm_pos = block.index('st.checkbox("内容を確認しました"')
    button_pos = block.index('st.button(\n            "確認のうえ取り込む"')
    write_pos = block.index("writer_repositories.provenance.import_maps_results(")
    assert confirm_pos < button_pos < write_pos
    button_call = block[button_pos:write_pos]
    assert "disabled=not confirmed" in button_call


def test_idempotency_key_checked_before_file_uploader_result_is_trusted():
    block = _cloud_maps_import_ui_source()
    key_pos = block.index("maps_import_idempotency_key(")
    applied_check_pos = block.index("applied_key == idempotency_key")
    write_pos = block.index("writer_repositories.provenance.import_maps_results(")
    assert key_pos < applied_check_pos < write_pos


def test_real_matching_preview_runs_before_the_write_call():
    """Unlike the Comdesk/厚生局 cloud panels' documented KNOWN GAP (Postgres
    dry-run matcher not available), the Google Maps matcher is Postgres-
    native, so this panel's preview must run the real matching logic
    (preview_maps_import), not just row/column counts."""
    block = _cloud_maps_import_ui_source()
    preview_pos = block.index("preview_maps_import(")
    write_pos = block.index("writer_repositories.provenance.import_maps_results(")
    assert preview_pos < write_pos
    assert "推定" in block  # explicitly labeled an estimate, not the confirmed result


def test_preview_estimate_is_never_shown_as_the_confirmed_result():
    block = _cloud_maps_import_ui_source()
    assert "推定" in block
    assert "確定結果" in block


def test_preview_and_confirmed_result_display_the_same_count_fields():
    """Review fix: an earlier draft showed a different, narrower vocabulary
    in the preview (NEW/MATCHED/AMBIGUOUS/EXCLUDED/ERROR, where MATCHED
    meant something different from the real result's MATCHED). Both
    displays must now read from the same counts keys -- the metrics block
    and the final success message must reference the same dict fields."""
    block = _cloud_maps_import_ui_source()
    for field in ("TOTAL", "WEBSITE", "NO_WEBSITE", "NOT_FOUND", "AMBIGUOUS", "EXCLUDED", "ERROR", "UNLINKED"):
        assert f"counts['{field}']" in block
        assert f"result.get('{field}'" in block


def test_load_table_call_is_wrapped_so_oversized_or_malformed_csv_shows_a_readable_error():
    """load_table() enforces a file-size cap and a row-count cap (raises
    ValueError rather than reading an unbounded file) -- this multi-operator
    cloud panel must turn that into st.error, not an unhandled traceback,
    unlike the single-user local Step4 uploader that calls the same
    function unguarded."""
    block = _cloud_maps_import_ui_source()
    load_pos = block.index("load_table(file_bytes, upload.name)")
    try_pos = block.rindex("try:", 0, load_pos)
    except_pos = block.index("except ValueError as exc:", load_pos)
    assert try_pos < load_pos < except_pos


def test_preview_call_is_wrapped_in_a_broad_exception_handler():
    """preview_maps_import() itself guarantees rollback via try/finally
    regardless of exception type (see test_maps_cloud_import.py); this is
    the separate, UI-level guarantee that any exception it raises (SQL
    error, matcher exception, anything) still produces a readable message
    instead of crashing the script."""
    block = _cloud_maps_import_ui_source()
    preview_pos = block.index("preview_maps_import(writer_repositories.provenance, maps_frame)")
    try_pos = block.rindex("try:", 0, preview_pos)
    except_pos = block.index("except Exception as exc:", preview_pos)
    assert try_pos < preview_pos < except_pos


def test_uses_dedicated_writer_connection_not_the_general_write_backend():
    block = _cloud_maps_import_ui_source()
    assert "maps_import_writer_repositories(" in block
    assert "write_repositories_for(store)" not in block
    writer_pos = block.index("maps_import_writer_repositories(")
    write_pos = block.index("writer_repositories.provenance.import_maps_results(")
    assert writer_pos < write_pos


def test_unconfigured_writer_connection_is_handled_before_any_write_attempt():
    block = _cloud_maps_import_ui_source()
    assert "MapsImportWriterNotConfiguredError" in block
    try_pos = block.index("try:")
    except_pos = block.index("except MapsImportWriterNotConfiguredError")
    write_pos = block.index("writer_repositories.provenance.import_maps_results(")
    assert try_pos < except_pos < write_pos


def test_no_misleading_never_writes_claim_once_the_panel_is_reachable():
    block = _cloud_maps_import_ui_source()
    assert "本番データは書込みません" not in block
    assert "実際に書き込みます" in block


def test_operator_email_and_idempotency_key_are_logged_on_apply():
    block = _cloud_maps_import_ui_source()
    assert "logging.info(" in block
    log_pos = block.index("logging.info(")
    write_pos = block.index("writer_repositories.provenance.import_maps_results(")
    assert write_pos < log_pos  # logged only after the write actually ran
    log_call = block[log_pos:block.index(")", block.index("result,", log_pos)) + 1]
    assert "operator_email" in log_call
    assert "idempotency_key" in log_call


def test_wired_into_step4_of_simple_workflow_ui_regardless_of_legacy_uploader_state():
    """Review fix lesson (Issue #130 PR #131): the sibling panel call must sit
    outside (after, at the function's own indent level) the `if maps_upload:`
    block that wraps the legacy path, so it renders even with no file
    selected in the unrelated legacy uploader."""
    source = _app_source()
    start = source.index("def simple_workflow_ui")
    end = source.index("def import_ui_comdesk_step", start)
    body = source[start:end]
    assert 'st.subheader("Step4：Google Maps結果を取り込む")' in body
    call = "    cloud_maps_import_ui(store, key_prefix=\"simple_maps\")\n"
    assert call in body
    assert not call.startswith("        ")  # 4-space indent == function body top level
    step4_pos = body.index('st.subheader("Step4：Google Maps結果を取り込む")')
    call_pos = body.index(call)
    step5_pos = body.index('st.subheader("Step5：HP・Email自動調査")')
    assert step4_pos < call_pos < step5_pos
