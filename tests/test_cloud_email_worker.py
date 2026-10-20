import pytest

from src.cloud_run import email_executor
from scripts import cloud_email_worker


class DispatchRepository:
    def __init__(self):
        self.status = "RUNNING"
        self.dispatch = None
    def begin_worker_dispatch(self, run_id, attempt_id):
        if self.dispatch and self.dispatch["status"] in {"REQUESTED", "UNKNOWN"}:
            raise ValueError("previous dispatch is unresolved")
        if self.status == "RUNNING" and self.dispatch and self.dispatch["status"] == "CONFIRMED":
            raise ValueError("already dispatched")
        self.status = "RUNNING"
        self.dispatch = {"status": "REQUESTED", "attempt_id": attempt_id}
    def finish_worker_dispatch(self, run_id, attempt_id, *, status, execution_name=""):
        assert self.dispatch["attempt_id"] == attempt_id
        self.dispatch.update(status=status)
        if execution_name:
            self.dispatch["execution_name"] = execution_name
        return True


def test_email_cloud_job_requires_explicit_bounded_configuration():
    with pytest.raises(email_executor.CloudEmailNotConfiguredError):
        email_executor.job_resource_name({})
    assert email_executor.max_clinics_per_execution({}) == 25
    with pytest.raises(email_executor.CloudEmailNotConfiguredError):
        email_executor.max_clinics_per_execution({email_executor.MAX_CLINICS_ENV: "51"})


def test_dispatch_passes_only_run_id_and_bounded_limit_and_never_retries():
    calls = []
    repo = DispatchRepository()
    response = email_executor.dispatch_email_worker(
        "run-123",
        env={"CLOUD_RUN_PROJECT": "test-project", "CLOUD_RUN_REGION": "asia-northeast1",
             email_executor.JOB_NAME_ENV: "email-worker", email_executor.MAX_CLINICS_ENV: "10"},
        execute_fn=lambda request: calls.append(request) or {"name": "operations/123"},
        dispatch_repository=repo,
    )
    assert response["name"] == "operations/123"
    assert calls[0]["body"]["overrides"]["containerOverrides"][0]["env"] == [
        {"name": "EMAIL_WORKER_RUN_ID", "value": "run-123"},
        {"name": email_executor.MAX_CLINICS_ENV, "value": "10"},
    ]
    assert repo.dispatch["status"] == "CONFIRMED"
    assert repo.dispatch["execution_name"] == "operations/123"
    repo = DispatchRepository()
    with pytest.raises(email_executor.CloudEmailNotConfiguredError, match="自動再起動は行わず"):
        email_executor.dispatch_email_worker(
            "run-123",
            env={"CLOUD_RUN_PROJECT": "test-project", "CLOUD_RUN_REGION": "asia-northeast1",
                 email_executor.JOB_NAME_ENV: "email-worker"},
            execute_fn=lambda _request: (_ for _ in ()).throw(TimeoutError()),
            dispatch_repository=repo,
        )
    assert repo.dispatch["status"] == "UNKNOWN"
    calls.clear()
    with pytest.raises(email_executor.EmailDispatchBlockedError, match="previous dispatch is unresolved"):
        email_executor.dispatch_email_worker(
            "run-123",
            env={"CLOUD_RUN_PROJECT": "test-project", "CLOUD_RUN_REGION": "asia-northeast1",
                 email_executor.JOB_NAME_ENV: "email-worker"},
            execute_fn=lambda request: calls.append(request),
            dispatch_repository=repo,
        )
    assert calls == []


def test_email_worker_refuses_retry_or_parallel_task(monkeypatch):
    monkeypatch.setenv("EMAIL_WORKER_RUN_ID", "run-123")
    with pytest.raises(RuntimeError, match="tasks=1"):
        cloud_email_worker.run(env={"EMAIL_WORKER_RUN_ID": "run-123", "CLOUD_RUN_TASK_INDEX": "1"},
                              runner_factory=lambda **_kwargs: None)


def test_email_worker_recovers_existing_run_and_joins_execution():
    events = []

    class Thread:
        def join(self):
            events.append("joined")

    class Runner:
        _thread = Thread()
        def recover(self, run_id):
            events.append(("recover", run_id))
            return True

    assert cloud_email_worker.run(env={"EMAIL_WORKER_RUN_ID": "run-123"},
                                  runner_factory=lambda **_kwargs: Runner()) == 0
    assert events == [("recover", "run-123"), "joined"]


def test_email_worker_duplicate_execution_is_standby():
    class Runner:
        _thread = None
        def recover(self, _run_id): return False

    assert cloud_email_worker.run(env={"EMAIL_WORKER_RUN_ID": "run-123"},
                                  runner_factory=lambda **_kwargs: Runner()) == 0
