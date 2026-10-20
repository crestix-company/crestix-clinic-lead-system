import pytest

from src.cloud_run import email_executor
from scripts import cloud_email_worker


def test_email_cloud_job_requires_explicit_bounded_configuration():
    with pytest.raises(email_executor.CloudEmailNotConfiguredError):
        email_executor.job_resource_name({})
    assert email_executor.max_clinics_per_execution({}) == 25
    with pytest.raises(email_executor.CloudEmailNotConfiguredError):
        email_executor.max_clinics_per_execution({email_executor.MAX_CLINICS_ENV: "51"})


def test_dispatch_passes_only_run_id_and_bounded_limit_and_never_retries():
    calls = []
    response = email_executor.dispatch_email_worker(
        "run-123",
        env={"CLOUD_RUN_PROJECT": "test-project", "CLOUD_RUN_REGION": "asia-northeast1",
             email_executor.JOB_NAME_ENV: "email-worker", email_executor.MAX_CLINICS_ENV: "10"},
        execute_fn=lambda request: calls.append(request) or {"name": "operations/123"},
    )
    assert response["name"] == "operations/123"
    assert calls[0]["body"]["overrides"]["containerOverrides"][0]["env"] == [
        {"name": "EMAIL_WORKER_RUN_ID", "value": "run-123"},
        {"name": email_executor.MAX_CLINICS_ENV, "value": "10"},
    ]
    with pytest.raises(email_executor.CloudEmailNotConfiguredError, match="自動再起動は行わず"):
        email_executor.dispatch_email_worker(
            "run-123",
            env={"CLOUD_RUN_PROJECT": "test-project", "CLOUD_RUN_REGION": "asia-northeast1",
                 email_executor.JOB_NAME_ENV: "email-worker"},
            execute_fn=lambda _request: (_ for _ in ()).throw(TimeoutError()),
        )


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
