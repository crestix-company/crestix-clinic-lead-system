"""Phase B: Cloud Run Jobs HP worker (scripts/cloud_hp_worker.py) と
src/cloud_run/job_dispatch.py のテスト。

実DB・実GCP・実ネットワークへは一切接続しない。store/DB接続/run_job等は
すべてfake/mockで注入する。
"""
import signal
import threading

import pytest

from scripts import cloud_hp_worker
from src.cloud_run import job_dispatch


VALID_JOB_ID = "0123456789abcdef0123456789abcdef"


def _base_env(**overrides):
    env = {
        "CLOUD_RUN_TASK_INDEX": "0",
        "CLOUD_RUN_TASK_ATTEMPT": "0",
        "HP_WORKER_JOB_ID": VALID_JOB_ID,
    }
    env.update(overrides)
    return env


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.last_sql = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, params=None):
        self.last_sql = sql
        if "pg_advisory_unlock" in sql:
            self.conn.unlock_calls += 1

    def fetchone(self):
        if "pg_try_advisory_lock" in self.last_sql:
            return (self.conn.lock_acquired,)
        return (True,)


class FakeLockConnection:
    def __init__(self, lock_acquired=True):
        self.lock_acquired = lock_acquired
        self.unlock_calls = 0

    def cursor(self):
        return FakeCursor(self)


class FakeJobsRepo:
    """Records calls and lets tests drive job_status()/run_job()/pause_job() behavior."""

    def __init__(self, initial_status, *, final_status=None):
        self.status_sequence = [initial_status] if final_status is None else [initial_status, final_status]
        self.run_job_calls = []
        self.pause_job_calls = []

    def job_status(self, store, job_id):
        status = self.status_sequence[-1] if len(self.status_sequence) == 1 else self.status_sequence.pop(0)
        return None if status is None else {"status": status, "id": job_id}

    def run_job(self, store, job_id, progress_callback):
        self.run_job_calls.append((store, job_id, progress_callback))

    def pause_job(self, store, job_id):
        self.pause_job_calls.append((store, job_id))


# ---- validate_execution_environment ----------------------------------------

def test_rejects_non_zero_task_index():
    with pytest.raises(cloud_hp_worker.WorkerConfigError):
        cloud_hp_worker.validate_execution_environment(_base_env(CLOUD_RUN_TASK_INDEX="1"))


def test_rejects_retried_task_attempt():
    with pytest.raises(cloud_hp_worker.WorkerConfigError, match="再試行"):
        cloud_hp_worker.validate_execution_environment(_base_env(CLOUD_RUN_TASK_ATTEMPT="1"))


def test_rejects_missing_job_id():
    with pytest.raises(cloud_hp_worker.WorkerConfigError):
        cloud_hp_worker.validate_execution_environment(_base_env(HP_WORKER_JOB_ID=""))


def test_rejects_malformed_job_id():
    with pytest.raises(cloud_hp_worker.WorkerConfigError):
        cloud_hp_worker.validate_execution_environment(_base_env(HP_WORKER_JOB_ID="not-a-job-id"))


def test_accepts_well_formed_environment():
    assert cloud_hp_worker.validate_execution_environment(_base_env()) == VALID_JOB_ID


# ---- run_worker: out-of-scope job rejection ---------------------------------

def test_refuses_to_rerun_completed_job():
    repo = FakeJobsRepo(initial_status="COMPLETED")
    lock_conn = FakeLockConnection()
    result = cloud_hp_worker.run_worker(
        env=_base_env(), store=object(), lock_conn=lock_conn,
        run_job_fn=repo.run_job, job_status_fn=repo.job_status, pause_job_fn=repo.pause_job,
        install_signal_handler=False,
    )
    assert result == {"job_id": VALID_JOB_ID, "outcome": "SKIPPED_COMPLETED", "ran": False}
    assert repo.run_job_calls == []


def test_refuses_to_run_reset_job():
    repo = FakeJobsRepo(initial_status="RESET")
    lock_conn = FakeLockConnection()
    result = cloud_hp_worker.run_worker(
        env=_base_env(), store=object(), lock_conn=lock_conn,
        run_job_fn=repo.run_job, job_status_fn=repo.job_status, pause_job_fn=repo.pause_job,
        install_signal_handler=False,
    )
    assert result["outcome"] == "SKIPPED_RESET"
    assert repo.run_job_calls == []


def test_raises_when_job_id_has_no_history():
    repo = FakeJobsRepo(initial_status=None)
    lock_conn = FakeLockConnection()
    with pytest.raises(cloud_hp_worker.WorkerConfigError):
        cloud_hp_worker.run_worker(
            env=_base_env(), store=object(), lock_conn=lock_conn,
            run_job_fn=repo.run_job, job_status_fn=repo.job_status, pause_job_fn=repo.pause_job,
            install_signal_handler=False,
        )


# ---- run_worker: cross-Execution double-start prevention --------------------

def test_refuses_to_run_when_advisory_lock_is_already_held():
    repo = FakeJobsRepo(initial_status="PAUSED")
    lock_conn = FakeLockConnection(lock_acquired=False)
    result = cloud_hp_worker.run_worker(
        env=_base_env(), store=object(), lock_conn=lock_conn,
        run_job_fn=repo.run_job, job_status_fn=repo.job_status, pause_job_fn=repo.pause_job,
        install_signal_handler=False,
    )
    assert result == {"job_id": VALID_JOB_ID, "outcome": "ALREADY_RUNNING_ELSEWHERE", "ran": False}
    assert repo.run_job_calls == []


def test_runs_and_releases_lock_on_success():
    repo = FakeJobsRepo(initial_status="PAUSED", final_status="COMPLETED")
    lock_conn = FakeLockConnection(lock_acquired=True)
    result = cloud_hp_worker.run_worker(
        env=_base_env(), store=object(), lock_conn=lock_conn,
        run_job_fn=repo.run_job, job_status_fn=repo.job_status, pause_job_fn=repo.pause_job,
        install_signal_handler=False,
    )
    assert result == {"job_id": VALID_JOB_ID, "outcome": "COMPLETED", "ran": True}
    assert len(repo.run_job_calls) == 1
    assert lock_conn.unlock_calls == 1


def test_lock_is_released_even_if_run_job_raises():
    repo = FakeJobsRepo(initial_status="PAUSED")

    def exploding_run_job(store, job_id, progress_callback):
        raise RuntimeError("boom")

    lock_conn = FakeLockConnection(lock_acquired=True)
    with pytest.raises(RuntimeError):
        cloud_hp_worker.run_worker(
            env=_base_env(), store=object(), lock_conn=lock_conn,
            run_job_fn=exploding_run_job, job_status_fn=repo.job_status, pause_job_fn=repo.pause_job,
            install_signal_handler=False,
        )
    assert lock_conn.unlock_calls == 1


# ---- run_worker: SIGTERM / timeout safe stop --------------------------------

def test_sigterm_triggers_progress_callback_false_and_pauses_interrupted_job():
    repo = FakeJobsRepo(initial_status="PAUSED", final_status="RUNNING")
    lock_conn = FakeLockConnection(lock_acquired=True)
    stop_event = threading.Event()

    captured_callback = {}

    def capturing_run_job(store, job_id, progress_callback):
        captured_callback["progress_callback"] = progress_callback
        # Simulate Cloud Run delivering SIGTERM mid-run.
        stop_event.set()
        assert progress_callback() is False

    store = object()
    result = cloud_hp_worker.run_worker(
        env=_base_env(), store=store, lock_conn=lock_conn,
        run_job_fn=capturing_run_job, job_status_fn=repo.job_status, pause_job_fn=repo.pause_job,
        stop_event=stop_event, install_signal_handler=False,
    )
    assert result == {"job_id": VALID_JOB_ID, "outcome": "INTERRUPTED_PAUSED", "ran": True}
    assert repo.pause_job_calls == [(store, VALID_JOB_ID)]


def test_sigterm_signal_handler_sets_stop_event():
    """A real SIGTERM delivered mid-run must flip progress_callback() to False,
    using cloud_hp_worker's own installed handler (not the process default,
    which would otherwise terminate the test runner)."""
    repo = FakeJobsRepo(initial_status="PAUSED", final_status="RUNNING")
    lock_conn = FakeLockConnection(lock_acquired=True)
    observed = {}

    def capturing_run_job(store, job_id, progress_callback):
        assert progress_callback() is True
        import os as _os
        _os.kill(_os.getpid(), signal.SIGTERM)
        observed["after_sigterm"] = progress_callback()

    try:
        result = cloud_hp_worker.run_worker(
            env=_base_env(), store=object(), lock_conn=lock_conn,
            run_job_fn=capturing_run_job, job_status_fn=repo.job_status, pause_job_fn=repo.pause_job,
            install_signal_handler=True,
        )
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
    assert observed["after_sigterm"] is False
    assert result["outcome"] == "INTERRUPTED_PAUSED"


# ---- job_dispatch: override hygiene + outcome classification ----------------

def test_build_job_run_request_only_overrides_job_id():
    request = job_dispatch.build_job_run_request("projects/p/locations/r/jobs/j", VALID_JOB_ID)
    env_overrides = request["body"]["overrides"]["containerOverrides"][0]["env"]
    assert env_overrides == [{"name": "HP_WORKER_JOB_ID", "value": VALID_JOB_ID}]


def test_build_job_run_request_rejects_non_job_id_shaped_values():
    with pytest.raises(job_dispatch.InvalidJobIdError):
        job_dispatch.build_job_run_request("projects/p/locations/r/jobs/j", "'; DROP TABLE x;--")


def test_run_hp_worker_job_success_is_dispatched():
    def fake_execute(request):
        return {
            "name": "projects/p/locations/r/operations/op-abc",
            "metadata": {"name": "projects/p/locations/r/jobs/j/executions/exec-xyz"},
        }

    result = job_dispatch.run_hp_worker_job(
        execute_fn=fake_execute, job_resource_name="projects/p/locations/r/jobs/j",
        clinic_job_id=VALID_JOB_ID,
    )
    assert result.status == job_dispatch.DISPATCHED
    # Operation and Execution are two different resources with two different
    # names -- never conflate them into a single field.
    assert result.operation_name == "projects/p/locations/r/operations/op-abc"
    assert result.execution_name == "projects/p/locations/r/jobs/j/executions/exec-xyz"


def test_run_hp_worker_job_dispatched_with_only_operation_name():
    """metadata.name can be briefly absent right as the Operation starts --
    the Operation name alone is still enough to reconcile against later."""
    def fake_execute(request):
        return {"name": "projects/p/locations/r/operations/op-abc"}

    result = job_dispatch.run_hp_worker_job(
        execute_fn=fake_execute, job_resource_name="projects/p/locations/r/jobs/j",
        clinic_job_id=VALID_JOB_ID,
    )
    assert result.status == job_dispatch.DISPATCHED
    assert result.operation_name == "projects/p/locations/r/operations/op-abc"
    assert result.execution_name is None


def test_run_hp_worker_job_timeout_is_unknown_not_rejected():
    def fake_execute(request):
        raise TimeoutError("deadline exceeded")

    result = job_dispatch.run_hp_worker_job(
        execute_fn=fake_execute, job_resource_name="projects/p/locations/r/jobs/j",
        clinic_job_id=VALID_JOB_ID,
    )
    assert result.status == job_dispatch.DISPATCH_UNKNOWN


def test_run_hp_worker_job_server_error_is_unknown():
    class FakeResp:
        status = 503

    class FakeHttpError(Exception):
        resp = FakeResp()

    def fake_execute(request):
        raise FakeHttpError("service unavailable")

    result = job_dispatch.run_hp_worker_job(
        execute_fn=fake_execute, job_resource_name="projects/p/locations/r/jobs/j",
        clinic_job_id=VALID_JOB_ID,
    )
    assert result.status == job_dispatch.DISPATCH_UNKNOWN


def test_run_hp_worker_job_client_error_is_rejected_not_unknown():
    class FakeResp:
        status = 404

    class FakeHttpError(Exception):
        resp = FakeResp()

    def fake_execute(request):
        raise FakeHttpError("job not found")

    result = job_dispatch.run_hp_worker_job(
        execute_fn=fake_execute, job_resource_name="projects/p/locations/r/jobs/j",
        clinic_job_id=VALID_JOB_ID,
    )
    assert result.status == job_dispatch.REJECTED


def test_run_hp_worker_job_missing_both_names_is_unknown():
    def fake_execute(request):
        return {}

    result = job_dispatch.run_hp_worker_job(
        execute_fn=fake_execute, job_resource_name="projects/p/locations/r/jobs/j",
        clinic_job_id=VALID_JOB_ID,
    )
    assert result.status == job_dispatch.DISPATCH_UNKNOWN


def test_run_hp_worker_job_never_forwards_extra_overrides():
    """Guards the 'overrides carry only the job id' contract end to end."""
    captured = {}

    def fake_execute(request):
        captured["request"] = request
        return {"name": "op/1", "metadata": {"name": "exec/1"}}

    job_dispatch.run_hp_worker_job(
        execute_fn=fake_execute, job_resource_name="projects/p/locations/r/jobs/j",
        clinic_job_id=VALID_JOB_ID,
    )
    overrides = captured["request"]["body"]["overrides"]
    assert list(overrides.keys()) == ["containerOverrides", "taskCount"]
    assert len(overrides["containerOverrides"]) == 1
    assert len(overrides["containerOverrides"][0]["env"]) == 1


# ---- job_dispatch: Operation reconciliation (never a new dispatch) --------

def test_reconcile_operation_without_a_name_is_still_unknown():
    assert job_dispatch.reconcile_operation(
        get_operation_fn=lambda name: (_ for _ in ()).throw(AssertionError("must not be called")),
        operation_name="",
    ) == job_dispatch.RECONCILE_STILL_UNKNOWN


def test_reconcile_operation_in_flight_is_running():
    result = job_dispatch.reconcile_operation(
        get_operation_fn=lambda name: {"name": name, "done": False},
        operation_name="projects/p/locations/r/operations/op-abc",
    )
    assert result == job_dispatch.RECONCILE_RUNNING


def test_reconcile_operation_done_without_error_is_running():
    """done=True with no error means the Execution genuinely started and (at
    least) ran to completion -- a fresh dispatch for the same job id is still
    forbidden; the caller must look at the Job's own DB status instead."""
    result = job_dispatch.reconcile_operation(
        get_operation_fn=lambda name: {"name": name, "done": True, "response": {}},
        operation_name="projects/p/locations/r/operations/op-abc",
    )
    assert result == job_dispatch.RECONCILE_RUNNING


def test_reconcile_operation_with_error_is_failed_and_safe_to_retry():
    result = job_dispatch.reconcile_operation(
        get_operation_fn=lambda name: {"name": name, "done": True, "error": {"code": 13, "message": "boom"}},
        operation_name="projects/p/locations/r/operations/op-abc",
    )
    assert result == job_dispatch.RECONCILE_FAILED


def test_reconcile_operation_lookup_failure_stays_unknown_never_raises():
    def exploding_get(name):
        raise TimeoutError("deadline exceeded")

    result = job_dispatch.reconcile_operation(
        get_operation_fn=exploding_get, operation_name="projects/p/locations/r/operations/op-abc",
    )
    assert result == job_dispatch.RECONCILE_STILL_UNKNOWN


def test_reconcile_operation_unusable_response_stays_unknown():
    result = job_dispatch.reconcile_operation(
        get_operation_fn=lambda name: None, operation_name="projects/p/locations/r/operations/op-abc",
    )
    assert result == job_dispatch.RECONCILE_STILL_UNKNOWN
