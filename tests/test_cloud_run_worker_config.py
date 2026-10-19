"""Cloud Run Jobs worker image (Dockerfile.cloudrun.worker) 設定・起動検証。

実Docker build・実GCP・実DBへは一切接続しない。Dockerfile/.dockerignoreのテキスト
内容と、scripts/cloud_hp_worker.main()が未設定環境で安全に（秘密値を出さず、
exit code 1で）止まることだけを検証する。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(name):
    return (ROOT / name).read_text(encoding="utf-8")


def test_worker_dockerfile_uses_python312_slim():
    text = _read("Dockerfile.cloudrun.worker")
    assert "FROM python:3.12-slim" in text


def test_worker_dockerfile_runs_as_non_root():
    text = _read("Dockerfile.cloudrun.worker")
    assert "USER clinicapp" in text
    assert "useradd" in text


def test_worker_dockerfile_invokes_the_worker_script_directly():
    text = _read("Dockerfile.cloudrun.worker")
    assert 'CMD ["python", "scripts/cloud_hp_worker.py"]' in text
    assert "COPY scripts/cloud_hp_worker.py" in text


def test_worker_dockerfile_never_runs_streamlit_or_listens_on_a_port():
    """A Cloud Run Job task must exit when done, not serve HTTP -- running
    app_v2.py or binding $PORT here would make it behave like a Service."""
    text = _read("Dockerfile.cloudrun.worker")
    assert "streamlit" not in text.lower()
    assert "\nEXPOSE" not in text
    assert "--server.port" not in text
    assert "0.0.0.0" not in text
    assert 'CMD ["python", "scripts/cloud_hp_worker.py"]' == text.strip().splitlines()[-1]


def test_worker_dockerfile_never_copies_data_or_secret_paths():
    text = _read("Dockerfile.cloudrun.worker")
    for forbidden in ("COPY data", "COPY .env", "COPY .supabase-runtime.env.local", "COPY code_backup"):
        assert forbidden not in text


def test_worker_dockerfile_uses_requirements_txt_not_lock():
    text = _read("Dockerfile.cloudrun.worker")
    assert "requirements.txt" in text
    assert "requirements-lock.txt" not in text


def test_dockerignore_allowlists_the_worker_entry_point():
    text = _read(".dockerignore")
    lines = [line.strip() for line in text.splitlines()]
    assert "*" in lines
    assert "!scripts/cloud_hp_worker.py" in lines


def test_dockerignore_still_excludes_real_clinic_data_and_credentials():
    text = _read(".dockerignore")
    for pattern in (
        "*.sqlite3*", "*.csv", ".env", ".supabase-runtime.env.local",
        "config/email_research.env.local", "config/gmail_oauth_client.json",
    ):
        assert pattern in text


# ---- container startup ("起動検証"): scripts.cloud_hp_worker.main() ---------

def test_worker_entrypoint_fails_closed_without_db_url(monkeypatch, capsys):
    """Equivalent to "docker run" with no SUPABASE_RUNTIME_DB_URL configured:
    must exit 1 immediately, print no secret, and never attempt a DB/network
    connection (no exception, no hang)."""
    from scripts import cloud_hp_worker

    monkeypatch.delenv("SUPABASE_RUNTIME_DB_URL", raising=False)
    exit_code = cloud_hp_worker.main([])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "SUPABASE_RUNTIME_DB_URL is not set" in captured.err
    assert "postgresql://" not in (captured.out + captured.err)


def test_worker_entrypoint_rejects_bad_execution_environment_before_any_db_work(monkeypatch):
    """validate_execution_environment() must be checked -- and reject -- before
    run_worker() ever acquires a lock or calls job_status/run_job."""
    from scripts import cloud_hp_worker

    called = []
    fake_store = object()

    result_or_error = None
    try:
        cloud_hp_worker.run_worker(
            env={"CLOUD_RUN_TASK_INDEX": "1", "CLOUD_RUN_TASK_ATTEMPT": "0", "HP_WORKER_JOB_ID": "0" * 32},
            store=fake_store,
            lock_conn=None,  # must never be touched: proves no lock acquisition is attempted
            run_job_fn=lambda *a, **k: called.append("run_job"),
            job_status_fn=lambda *a, **k: called.append("job_status"),
            pause_job_fn=lambda *a, **k: called.append("pause_job"),
            install_signal_handler=False,
        )
    except cloud_hp_worker.WorkerConfigError as exc:
        result_or_error = exc
    assert result_or_error is not None
    assert called == []
