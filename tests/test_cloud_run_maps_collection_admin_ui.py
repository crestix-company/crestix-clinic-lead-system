"""Issue #130: source-level safety and scope-boundary checks for
cloud_maps_collection_admin_ui(), mirroring this repo's established pattern
(test_cloud_run_maps_import_ui.py etc.) of verifying UI-gating structure
directly from source."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _app_source():
    return (ROOT / "app_v2.py").read_text(encoding="utf-8")


def _admin_ui_source():
    source = _app_source()
    start = source.index("def cloud_maps_collection_admin_ui(")
    end = source.index("\ndef ", start + 10)
    return source[start:end]


def test_returns_immediately_under_read_only_before_any_widget_or_write_call():
    block = _admin_ui_source()
    guard_pos = block.index("if read_only_ui_mode() or not is_cloud_run_deployment():")
    return_pos = block.index("return", guard_pos)
    body_before_return = block[:return_pos]
    for forbidden in ("st.file_uploader(", "st.button(", "maps_collection_tasks_repository()", "write_repositories_for("):
        assert forbidden not in body_before_return


def test_non_cloud_run_process_is_also_blocked():
    block = _admin_ui_source()
    assert "or not is_cloud_run_deployment()" in block
    guard_pos = block.index("if read_only_ui_mode() or not is_cloud_run_deployment():")
    flag_pos = block.index('cloud_write_feature_enabled("CLINIC_CLOUD_MAPS_COLLECTION_ADMIN_ENABLED")')
    assert guard_pos < flag_pos


def test_feature_flag_checked_before_operator_identity_and_task_repo():
    block = _admin_ui_source()
    flag_pos = block.index('cloud_write_feature_enabled("CLINIC_CLOUD_MAPS_COLLECTION_ADMIN_ENABLED")')
    identity_pos = block.index("operator_email_from_headers(")
    repo_pos = block.index("maps_collection_tasks_repository()")
    assert flag_pos < identity_pos < repo_pos


def test_operator_authorization_checked_before_task_repo_access():
    block = _admin_ui_source()
    guard_pos = block.index("if not operator_authorized_for_cloud_write(operator_email):")
    repo_pos = block.index("maps_collection_tasks_repository()")
    assert guard_pos < repo_pos


def test_identity_presence_alone_is_not_treated_as_authorization():
    block = _admin_ui_source()
    assert "operator_authorized_for_cloud_write(operator_email)" in block
    assert "if not operator_email:" not in block


def test_uses_dedicated_task_state_writer_not_the_general_write_backend():
    block = _admin_ui_source()
    assert "maps_collection_tasks_repository()" in block
    assert "write_repositories_for(store)" not in block


def test_task_completion_reuses_the_existing_maps_import_writer_not_a_duplicate():
    """Issue #130 point 7/9: the result-CSV-upload section must call the
    SAME functions cloud_maps_import_ui() already uses (not reimplement
    them), via a connection kept deliberately separate from the task-state
    writer above (src.cloud_run.maps_collection_writer's docstring)."""
    block = _admin_ui_source()
    assert "maps_import_writer_repositories(" in block
    assert "preview_maps_import(" in block
    assert "import_maps_results(" in block
    # Both dedicated writers appear -- this feature's own task-state role,
    # and the pre-existing, separate Maps-import role -- never the general one.
    assert "maps_collection_tasks_repository()" in block
    assert "write_repositories_for(" not in block


def test_never_calls_any_browser_automation_or_external_maps_api():
    """Sanity pin for the explicit scope boundary: this function must never
    itself script or call out to Google Maps / a headless browser / the
    Places API -- collection stays 100% manual via the unchanged extension."""
    block = _admin_ui_source()
    for forbidden in (
        "selenium", "playwright", "puppeteer", "webdriver",
        "requests.get(", "requests.post(", "maps.googleapis.com", "places_api",
    ):
        assert forbidden not in block.casefold()


def test_task_completion_requires_clinic_overlap_before_import_is_even_attempted():
    block = _admin_ui_source()
    task_ids_pos = block.index("task_clinic_ids = set(selected_task[\"clinic_ids\"])")
    overlap_pos = block.index("overlap = uploaded_clinic_ids & task_clinic_ids")
    membership_guard_pos = block.index("if not uploaded_clinic_ids or not uploaded_clinic_ids <= task_clinic_ids:")
    writer_pos = block.index("import_writer_repositories = maps_import_writer_repositories()")
    assert task_ids_pos < overlap_pos < membership_guard_pos < writer_pos
    assert "not uploaded_clinic_ids" in block[membership_guard_pos:writer_pos]
    assert "not uploaded_clinic_ids <= task_clinic_ids" in block[membership_guard_pos:writer_pos]


def test_wired_into_step3_of_simple_workflow_ui():
    source = _app_source()
    start = source.index("def simple_workflow_ui")
    end = source.index("def import_ui_comdesk_step", start)
    body = source[start:end]
    assert 'st.subheader("Step3：Google Maps Collector")' in body
    call = '    cloud_maps_collection_admin_ui(store, key_prefix="simple_maps_collection")\n'
    assert call in body
    assert not call.startswith("        ")
    step3_pos = body.index('st.subheader("Step3：Google Maps Collector")')
    call_pos = body.index(call)
    step4_pos = body.index('st.subheader("Step4：Google Maps結果を取り込む")')
    assert step3_pos < call_pos < step4_pos
