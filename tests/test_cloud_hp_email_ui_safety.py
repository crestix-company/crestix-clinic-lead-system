from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_hp_cloud_controls_are_cloud_only_flagged_and_operator_gated_before_actions():
    source = _source("app_v2.py")
    start = source.index("def cloud_hp_job_controls_ui(")
    end = source.index("\ndef simple_workflow_ui", start)
    block = source[start:end]
    assert "if read_only or not is_cloud_run_deployment():" in block
    assert block.index('cloud_write_feature_enabled("CLINIC_CLOUD_HP_JOB_CONTROL_ENABLED")') < block.index("operator_email_from_headers(")
    assert block.index("operator_authorized_for_cloud_write(email)") < block.index("_create_cloud_hp_job(")
    assert block.index('hp_executor_mode() != "cloud_run"') < block.index("_create_cloud_hp_job(")
    assert "max_single_job_count(\"cloud_run\")" in block
    assert "precheck_budget(actual)" in block
    assert "_pause_cloud_hp_job(current[\"id\"])" in block
    assert "_reconcile_cloud_hp_job(current[\"id\"])" in block
    assert "reset_job(" not in block
    assert "auto_runner" not in block


def test_hp_resume_fails_closed_when_dispatch_status_cannot_be_read():
    source = _source("app_v2.py")
    start = source.index("def cloud_hp_job_controls_ui(")
    end = source.index("\ndef simple_workflow_ui", start)
    block = source[start:end]
    status_pos = block.index("dispatch_state = get_dispatch_state(current[\"id\"])")
    missing_guard = block.index("if dispatch_state is None:", status_pos)
    resume_pos = block.index('key="cloud_hp_resume"', status_pos)
    assert status_pos < missing_guard < resume_pos


def test_email_cloud_controls_require_operator_and_never_fall_back_to_local_runner():
    source = _source("src/email_research_ui.py")
    start = source.index("def _email_auto_research_controls(")
    end = source.index("\n\n@st.fragment", start)
    block = source[start:end]
    assert block.index("if cloud_deployment:") < block.index("repositories = email_repositories_for_ui()")
    assert "operator_authorized_for_cloud_write(operator_email)" in block
    assert 'cloud_write_feature_enabled("EMAIL_AUTO_RUN_ENABLED")' in block
    assert "elif cloud_email_configured:" in block
    assert "not cloud_deployment else None" in block
    assert "ローカル実行へ切り替えていません" in block


def test_cloud_run_page_exposes_only_dedicated_hp_and_email_controls():
    source = _source("app_v2.py")
    start = source.index("def simple_workflow_ui(")
    end = source.index("\ndef import_ui_comdesk_step", start)
    block = source[start:end]
    cloud_pos = block.index("elif is_cloud_run_deployment():")
    assert cloud_pos < block.index("cloud_hp_job_controls_ui(store, read_only=read_only)")
    assert cloud_pos < block.index("email_auto_research_card(store, demo=demo)")
    assert cloud_pos < block.index("cloud_email_human_review_ui(store, read_only=read_only)")
    assert "hp_human_review_card(store)" not in block[cloud_pos:block.index("else:", cloud_pos)]


def test_cloud_email_human_review_is_flagged_and_operator_gated_before_repository_ui():
    source = _source("app_v2.py")
    start = source.index("def cloud_email_human_review_ui(")
    end = source.index("\ndef simple_workflow_ui", start)
    block = source[start:end]
    assert "if read_only or not is_cloud_run_deployment():" in block
    flag_pos = block.index('cloud_write_feature_enabled("CLINIC_CLOUD_EMAIL_HUMAN_REVIEW_ENABLED")')
    auth_pos = block.index("operator_authorized_for_cloud_write(email)")
    card_pos = block.index("email_human_review_card(store)")
    dialog_pos = block.index("email_human_review_dialog(store)")
    assert flag_pos < auth_pos < card_pos < dialog_pos
