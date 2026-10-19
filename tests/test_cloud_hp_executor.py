"""Phase C: CLINIC_HP_EXECUTOR=cloud_run 分岐（src.cloud_run.hp_executor、app_v2.py連携）のテスト。

実GCP・実Google認証・実DBへは一切接続しない。
"""
import pytest

from src.cloud_run import hp_executor


def test_default_mode_is_local_when_unset():
    assert hp_executor.hp_executor_mode({}) == hp_executor.LOCAL
    assert hp_executor.is_cloud_run_executor({}) is False


def test_explicit_cloud_run_mode():
    env = {"CLINIC_HP_EXECUTOR": "cloud_run"}
    assert hp_executor.hp_executor_mode(env) == hp_executor.CLOUD_RUN
    assert hp_executor.is_cloud_run_executor(env) is True


def test_rejects_unknown_mode_value():
    with pytest.raises(ValueError):
        hp_executor.hp_executor_mode({"CLINIC_HP_EXECUTOR": "something_else"})


def test_max_single_job_count_caps_cloud_run_to_50_but_keeps_local_at_500():
    assert hp_executor.max_single_job_count(hp_executor.LOCAL) == 500
    assert hp_executor.max_single_job_count(hp_executor.CLOUD_RUN) == 50


def test_unlimited_auto_run_is_local_only():
    assert hp_executor.allow_unlimited_auto_run(hp_executor.LOCAL) is True
    assert hp_executor.allow_unlimited_auto_run(hp_executor.CLOUD_RUN) is False


def test_job_resource_name_requires_all_three_env_vars():
    with pytest.raises(hp_executor.CloudRunNotConfiguredError):
        hp_executor.job_resource_name({})
    with pytest.raises(hp_executor.CloudRunNotConfiguredError):
        hp_executor.job_resource_name({"CLOUD_RUN_PROJECT": "p", "CLOUD_RUN_REGION": "r"})


def test_job_resource_name_builds_expected_path():
    env = {
        "CLOUD_RUN_PROJECT": "my-project",
        "CLOUD_RUN_REGION": "asia-northeast1",
        "CLOUD_RUN_HP_WORKER_JOB_NAME": "clinic-lead-hp-worker",
    }
    assert hp_executor.job_resource_name(env) == (
        "projects/my-project/locations/asia-northeast1/jobs/clinic-lead-hp-worker"
    )


def test_dispatch_hp_job_uses_injected_execute_fn_and_never_touches_google_auth():
    env = {
        "CLOUD_RUN_PROJECT": "my-project",
        "CLOUD_RUN_REGION": "asia-northeast1",
        "CLOUD_RUN_HP_WORKER_JOB_NAME": "clinic-lead-hp-worker",
    }
    captured = {}

    def fake_execute(request):
        captured["request"] = request
        return {"metadata": {"name": "op/1"}}

    job_id = "0123456789abcdef0123456789abcdef"
    result = hp_executor.dispatch_hp_job(job_id, env=env, execute_fn=fake_execute)
    assert result.status == "DISPATCHED"
    assert captured["request"]["name"] == (
        "projects/my-project/locations/asia-northeast1/jobs/clinic-lead-hp-worker"
    )


def test_dispatch_hp_job_without_execute_fn_requires_cloud_run_config_first():
    """default_execute_fn() must not be constructed/called until job_resource_name succeeds,
    so an unconfigured deployment fails with a clear config error, never a google-auth error."""
    with pytest.raises(hp_executor.CloudRunNotConfiguredError):
        hp_executor.dispatch_hp_job("0123456789abcdef0123456789abcdef", env={})


def test_hp_executor_never_imports_google_api_client_at_module_level():
    """Google API client construction must stay inside default_execute_fn()'s
    inner closure (only imported when a real dispatch call is actually made),
    never at module import time -- otherwise importing this module in a test
    or in app_v2.py would require google-auth credentials/network access."""
    import ast
    from pathlib import Path

    source = Path(hp_executor.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    module_level_names = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            module_level_names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module_level_names.append(node.module or "")
    forbidden = [name for name in module_level_names if "googleapiclient" in name or "google.auth" in name]
    assert forbidden == []


# ---- app_v2.py wiring -------------------------------------------------------

def test_app_v2_imports_hp_executor_helpers():
    import app_v2
    assert app_v2.hp_executor_mode is hp_executor.hp_executor_mode
    assert app_v2.is_cloud_run_executor is hp_executor.is_cloud_run_executor
    assert app_v2.max_single_job_count is hp_executor.max_single_job_count
    assert app_v2.allow_unlimited_auto_run is hp_executor.allow_unlimited_auto_run
    assert app_v2.dispatch_hp_job is hp_executor.dispatch_hp_job


def test_dispatch_cloud_hp_job_records_dispatched_message(monkeypatch):
    import app_v2
    from src.cloud_run.job_dispatch import DispatchResult, DISPATCHED

    monkeypatch.setattr(
        "src.cloud_run.hp_executor.dispatch_hp_job",
        lambda jid, **kwargs: DispatchResult(status=DISPATCHED, execution_name="op/123"),
    )
    app_v2.st.session_state.clear()
    app_v2._dispatch_cloud_hp_job("0123456789abcdef0123456789abcdef")
    level, text = app_v2.st.session_state["cloud_hp_dispatch_message"]
    assert level == "success"
    assert "op/123" in text


def test_dispatch_cloud_hp_job_records_unknown_message_without_implying_success(monkeypatch):
    import app_v2
    from src.cloud_run.job_dispatch import DispatchResult, DISPATCH_UNKNOWN

    monkeypatch.setattr(
        "src.cloud_run.hp_executor.dispatch_hp_job",
        lambda jid, **kwargs: DispatchResult(status=DISPATCH_UNKNOWN, detail="timeout"),
    )
    app_v2.st.session_state.clear()
    app_v2._dispatch_cloud_hp_job("0123456789abcdef0123456789abcdef")
    level, text = app_v2.st.session_state["cloud_hp_dispatch_message"]
    assert level == "warning"
    assert "再実行" in text


def test_dispatch_cloud_hp_job_records_rejected_message(monkeypatch):
    import app_v2
    from src.cloud_run.job_dispatch import DispatchResult, REJECTED

    monkeypatch.setattr(
        "src.cloud_run.hp_executor.dispatch_hp_job",
        lambda jid, **kwargs: DispatchResult(status=REJECTED, detail="not found"),
    )
    app_v2.st.session_state.clear()
    app_v2._dispatch_cloud_hp_job("0123456789abcdef0123456789abcdef")
    level, _text = app_v2.st.session_state["cloud_hp_dispatch_message"]
    assert level == "error"


def test_dispatch_cloud_hp_job_surfaces_not_configured_error(monkeypatch):
    import app_v2
    from src.cloud_run.hp_executor import CloudRunNotConfiguredError

    def raise_not_configured(jid, **kwargs):
        raise CloudRunNotConfiguredError("CLOUD_RUN_PROJECT/CLOUD_RUN_REGION/CLOUD_RUN_HP_WORKER_JOB_NAMEが未設定です。")

    monkeypatch.setattr("src.cloud_run.hp_executor.dispatch_hp_job", raise_not_configured)
    app_v2.st.session_state.clear()
    app_v2._dispatch_cloud_hp_job("0123456789abcdef0123456789abcdef")
    level, text = app_v2.st.session_state["cloud_hp_dispatch_message"]
    assert level == "error"
    assert "CLOUD_RUN_PROJECT" in text
