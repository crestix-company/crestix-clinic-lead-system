"""Phase C: CLINIC_HP_EXECUTOR=cloud_run 分岐（src.cloud_run.hp_executor、app_v2.py連携）のテスト。

実GCP・実Google認証・実DBへは一切接続しない。
"""
import pytest

from src.cloud_run import hp_executor
from tests.test_cloud_run_job_control import FakeSettingsConnection


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


CLOUD_RUN_ENV = {
    "CLOUD_RUN_PROJECT": "my-project",
    "CLOUD_RUN_REGION": "asia-northeast1",
    "CLOUD_RUN_HP_WORKER_JOB_NAME": "clinic-lead-hp-worker",
}
JOB_ID = "0123456789abcdef0123456789abcdef"


def test_dispatch_hp_job_uses_injected_execute_fn_and_never_touches_google_auth():
    captured = {}

    def fake_execute(request):
        captured["request"] = request
        return {"name": "op/1", "metadata": {"name": "exec/1"}}

    conn = FakeSettingsConnection()
    result = hp_executor.dispatch_hp_job(
        JOB_ID, clinic_count=25, env=CLOUD_RUN_ENV, execute_fn=fake_execute, job_control_conn=conn,
    )
    assert result.status == "DISPATCHED"
    assert captured["request"]["name"] == (
        "projects/my-project/locations/asia-northeast1/jobs/clinic-lead-hp-worker"
    )


def test_dispatch_hp_job_persists_outcome_and_records_usage_only_on_dispatched():
    from src.cloud_run.dispatch_state import STATUS_DISPATCHED, get_dispatch_state
    from src.cloud_run.usage_budget import read_usage

    conn = FakeSettingsConnection()
    hp_executor.dispatch_hp_job(
        JOB_ID, clinic_count=25, env=CLOUD_RUN_ENV,
        execute_fn=lambda request: {"name": "op/1", "metadata": {"name": "exec/1"}},
        job_control_conn=conn,
    )
    state = get_dispatch_state(conn, JOB_ID)
    assert state["status"] == STATUS_DISPATCHED
    assert read_usage(conn) == {"executions": 1, "clinics": 25}


def test_dispatch_hp_job_is_blocked_while_a_prior_unknown_is_unreconciled():
    from src.cloud_run.dispatch_state import STATUS_DISPATCH_UNKNOWN, record_dispatch_state
    from src.cloud_run.usage_budget import read_usage

    conn = FakeSettingsConnection(job_statuses={JOB_ID: "RUNNING"})
    record_dispatch_state(conn, JOB_ID, status=STATUS_DISPATCH_UNKNOWN, operation_name="op/stale")

    def must_not_be_called(request):
        raise AssertionError("the Jobs API must never be called while BLOCKED_PENDING_RECONCILE")

    result = hp_executor.dispatch_hp_job(
        JOB_ID, clinic_count=25, env=CLOUD_RUN_ENV, execute_fn=must_not_be_called, job_control_conn=conn,
    )
    assert result.status == hp_executor.BLOCKED_PENDING_RECONCILE
    assert read_usage(conn) == {"executions": 0, "clinics": 0}


def test_dispatch_hp_job_is_refused_once_monthly_budget_is_exhausted():
    from src.cloud_run.usage_budget import read_usage, reserve_dispatch_usage

    conn = FakeSettingsConnection()
    env = dict(CLOUD_RUN_ENV, CLOUD_RUN_MAX_MONTHLY_EXECUTIONS="1")
    reserve_dispatch_usage(conn, 10, env=env)

    def must_not_be_called(request):
        raise AssertionError("the Jobs API must never be called once the monthly budget is exhausted")

    result = hp_executor.dispatch_hp_job(
        JOB_ID, clinic_count=5, env=env, execute_fn=must_not_be_called, job_control_conn=conn,
    )
    assert result.status == hp_executor.BUDGET_EXCEEDED
    # The refused attempt must not have additionally consumed budget.
    assert read_usage(conn) == {"executions": 1, "clinics": 10}


def test_dispatch_hp_job_releases_reserved_budget_on_clean_rejection():
    from src.cloud_run.usage_budget import read_usage

    conn = FakeSettingsConnection()

    class FakeResp:
        status = 404

    class FakeHttpError(Exception):
        resp = FakeResp()

    def rejected(request):
        raise FakeHttpError("job not found")

    result = hp_executor.dispatch_hp_job(
        JOB_ID, clinic_count=25, env=CLOUD_RUN_ENV, execute_fn=rejected, job_control_conn=conn,
    )
    assert result.status == "REJECTED"
    assert read_usage(conn) == {"executions": 0, "clinics": 0}


def test_dispatch_hp_job_does_not_release_budget_on_ambiguous_unknown():
    from src.cloud_run.usage_budget import read_usage

    conn = FakeSettingsConnection()

    def timeout(request):
        raise TimeoutError("deadline exceeded")

    result = hp_executor.dispatch_hp_job(
        JOB_ID, clinic_count=25, env=CLOUD_RUN_ENV, execute_fn=timeout, job_control_conn=conn,
    )
    assert result.status == "DISPATCH_UNKNOWN"
    # Ambiguous outcomes stay counted -- fail safe toward never exceeding budget.
    assert read_usage(conn) == {"executions": 1, "clinics": 25}


def test_precheck_budget_runs_before_any_job_row_would_be_created():
    from src.cloud_run.usage_budget import reserve_dispatch_usage

    conn = FakeSettingsConnection()
    env = {"CLOUD_RUN_MAX_MONTHLY_CLINICS": "30"}
    reserve_dispatch_usage(conn, 20, env=env)
    allowed, reason = hp_executor.precheck_budget(20, env=env, job_control_conn=conn)
    assert allowed is False
    assert reason != ""


def test_reconcile_hp_job_dispatch_resolves_unknown_to_confirmed_running():
    from src.cloud_run.dispatch_state import STATUS_CONFIRMED_RUNNING, STATUS_DISPATCH_UNKNOWN, record_dispatch_state

    conn = FakeSettingsConnection()
    record_dispatch_state(conn, JOB_ID, status=STATUS_DISPATCH_UNKNOWN, operation_name="op/1")
    resolved = hp_executor.reconcile_hp_job_dispatch(
        JOB_ID, get_operation_fn=lambda name: {"name": name, "done": False}, job_control_conn=conn,
    )
    assert resolved["status"] == STATUS_CONFIRMED_RUNNING


def test_get_dispatch_state_wrapper_reads_the_persisted_outcome():
    conn = FakeSettingsConnection()
    hp_executor.dispatch_hp_job(
        JOB_ID, clinic_count=25, env=CLOUD_RUN_ENV,
        execute_fn=lambda request: {"name": "op/1", "metadata": {"name": "exec/1"}},
        job_control_conn=conn,
    )
    state = hp_executor.get_dispatch_state(JOB_ID, job_control_conn=conn)
    assert state["status"] == "DISPATCHED"


def test_dispatch_is_fire_and_forget_not_tied_to_the_calling_request():
    """A Cloud Run Job Execution must keep running after the triggering HTTP
    request/browser session ends -- dispatch_hp_job must not block waiting for
    the Execution, only for the single `run` API call to return."""
    import time

    conn = FakeSettingsConnection()
    call_count = {"n": 0}

    def fake_execute(request):
        call_count["n"] += 1
        return {"name": "op/1", "metadata": {"name": "exec/1"}}

    start = time.monotonic()
    hp_executor.dispatch_hp_job(
        JOB_ID, clinic_count=25, env=CLOUD_RUN_ENV, execute_fn=fake_execute, job_control_conn=conn,
    )
    elapsed = time.monotonic() - start
    assert call_count["n"] == 1
    assert elapsed < 1.0  # no polling/sleep loop waiting for the Execution itself


def test_full_unknown_then_reconcile_then_retry_scenario():
    """End-to-end scenario covering PR review item 4: a dispatch that comes
    back DISPATCH_UNKNOWN must block any further dispatch for the same job id
    until reconciled, and only then (once reconciled as failed) allow a fresh
    dispatch -- at no point does a second Execution get started while the
    first is unresolved.

    Seeds the "unknown, with an Operation name" state directly via
    record_dispatch_state(), since that is the realistic reconcilable case
    (an ambiguous error after the Operation was created). A pure connection
    timeout with zero response received carries no Operation name at all and
    is a known, documented limit of operation-based reconciliation -- see
    job_dispatch.reconcile_operation's docstring; recovering that case would
    need a different strategy (listing Executions for the Job, not polling
    one Operation) and is not implemented here.
    """
    from src.cloud_run.dispatch_state import STATUS_DISPATCH_UNKNOWN, STATUS_SAFE_TO_RETRY, record_dispatch_state

    conn = FakeSettingsConnection(job_statuses={JOB_ID: "RUNNING"})
    calls = []

    record_dispatch_state(
        conn, JOB_ID, status=STATUS_DISPATCH_UNKNOWN,
        operation_name="projects/p/locations/r/operations/op-ambiguous",
    )

    def must_not_be_called(request):
        raise AssertionError("retry attempted before reconciliation")

    blocked = hp_executor.dispatch_hp_job(
        JOB_ID, clinic_count=25, env=CLOUD_RUN_ENV, execute_fn=must_not_be_called, job_control_conn=conn,
    )
    assert blocked.status == hp_executor.BLOCKED_PENDING_RECONCILE
    assert calls == []

    resolved = hp_executor.reconcile_hp_job_dispatch(
        JOB_ID, get_operation_fn=lambda name: {"name": name, "done": True, "error": {"code": 13}},
        job_control_conn=conn,
    )
    assert resolved["status"] == STATUS_SAFE_TO_RETRY

    def second_attempt_succeeds(request):
        calls.append("retry")
        return {"name": "op/2", "metadata": {"name": "exec/2"}}

    retried = hp_executor.dispatch_hp_job(
        JOB_ID, clinic_count=25, env=CLOUD_RUN_ENV, execute_fn=second_attempt_succeeds, job_control_conn=conn,
    )
    assert retried.status == "DISPATCHED"
    assert calls == ["retry"]


def test_reconcile_hp_job_dispatch_never_starts_a_new_execution():
    """reconcile_hp_job_dispatch takes get_operation_fn, never execute_fn --
    there is no code path here that can call jobs().run()."""
    import inspect
    assert "execute_fn" not in inspect.signature(hp_executor.reconcile_hp_job_dispatch).parameters


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
