"""Tests for src/cloud_run/job_control.py, dispatch_state.py, usage_budget.py.

Real DB/GCP/network access is never used: app_config.settings is simulated
in-memory with FakeSettingsConnection, matching the INSERT/SELECT/UPDATE
statements those modules actually issue.
"""
import json
from datetime import date

import pytest

from src.cloud_run import dispatch_state, job_control, usage_budget


class FakeSettingsCursor:
    def __init__(self, conn):
        self.conn = conn
        self._last_result = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, params=None):
        sql_norm = " ".join(sql.split())
        if sql_norm.startswith("INSERT INTO app_config.settings") and "DO NOTHING" in sql_norm:
            key, value = params
            if key not in self.conn.store:
                self.conn.store[key] = json.loads(value) if isinstance(value, str) else value
            self._last_result = None
        elif sql_norm.startswith("INSERT INTO app_config.settings") and "DO UPDATE" in sql_norm:
            key, value = params
            self.conn.store[key] = json.loads(value) if isinstance(value, str) else value
            self._last_result = None
        elif sql_norm.startswith("SELECT value FROM app_config.settings WHERE key=%s"):
            (key,) = params
            value = self.conn.store.get(key)
            self._last_result = (value,) if value is not None else None
        elif sql_norm.startswith("UPDATE app_config.settings SET value=%s WHERE key=%s"):
            value, key = params
            self.conn.store[key] = json.loads(value) if isinstance(value, str) else value
            self._last_result = None
        else:
            raise AssertionError(f"unexpected SQL in fake: {sql!r}")

    def fetchone(self):
        return self._last_result


class FakeSettingsConnection:
    def __init__(self):
        self.store = {}
        self.commits = 0

    def cursor(self):
        return FakeSettingsCursor(self)

    def commit(self):
        self.commits += 1


# ---- job_control.py ---------------------------------------------------------

def test_job_control_db_url_prefers_dedicated_role(monkeypatch):
    env = {
        "SUPABASE_JOB_CONTROL_DB_URL": "postgresql://job-control-role@db/postgres",
        "SUPABASE_RUNTIME_DB_URL": "postgresql://general-write-role@db/postgres",
    }
    assert job_control.job_control_db_url(env) == "postgresql://job-control-role@db/postgres"
    assert job_control.using_dedicated_job_control_role(env) is True


def test_job_control_db_url_falls_back_to_runtime_url_when_dedicated_role_unset():
    env = {"SUPABASE_RUNTIME_DB_URL": "postgresql://general-write-role@db/postgres"}
    assert job_control.job_control_db_url(env) == "postgresql://general-write-role@db/postgres"
    assert job_control.using_dedicated_job_control_role(env) is False


def test_job_control_db_url_raises_when_both_unset():
    with pytest.raises(job_control.JobControlNotConfiguredError):
        job_control.job_control_db_url({})


def test_job_control_write_repositories_uses_the_resolved_url():
    captured = {}

    def fake_builder(backend, *, supabase_url):
        captured["backend"] = backend
        captured["supabase_url"] = supabase_url
        return "repositories-sentinel"

    env = {"SUPABASE_JOB_CONTROL_DB_URL": "postgresql://job-control-role@db/postgres"}
    result = job_control.job_control_write_repositories(env, build_write_repositories=fake_builder)
    assert result == "repositories-sentinel"
    assert captured["supabase_url"] == "postgresql://job-control-role@db/postgres"
    assert captured["backend"] == job_control.WRITE_BACKEND_SUPABASE


# ---- dispatch_state.py ------------------------------------------------------

def test_may_dispatch_is_true_when_nothing_recorded_yet():
    conn = FakeSettingsConnection()
    assert dispatch_state.may_dispatch(conn, "job-1") is True


def test_may_dispatch_is_false_while_unknown_is_unreconciled():
    conn = FakeSettingsConnection()
    dispatch_state.record_dispatch_state(conn, "job-1", status=dispatch_state.STATUS_DISPATCH_UNKNOWN,
                                          operation_name="op/1")
    assert dispatch_state.may_dispatch(conn, "job-1") is False


def test_may_dispatch_is_false_while_confirmed_running():
    conn = FakeSettingsConnection()
    dispatch_state.record_dispatch_state(conn, "job-1", status=dispatch_state.STATUS_CONFIRMED_RUNNING)
    assert dispatch_state.may_dispatch(conn, "job-1") is False


@pytest.mark.parametrize("status", [dispatch_state.STATUS_SAFE_TO_RETRY, dispatch_state.STATUS_REJECTED])
def test_may_dispatch_is_true_for_retryable_statuses(status):
    conn = FakeSettingsConnection()
    dispatch_state.record_dispatch_state(conn, "job-1", status=status)
    assert dispatch_state.may_dispatch(conn, "job-1") is True


def test_get_dispatch_state_roundtrip():
    conn = FakeSettingsConnection()
    dispatch_state.record_dispatch_state(
        conn, "job-1", status=dispatch_state.STATUS_DISPATCHED,
        operation_name="op/1", execution_name="exec/1", detail="ok",
    )
    state = dispatch_state.get_dispatch_state(conn, "job-1")
    assert state["status"] == dispatch_state.STATUS_DISPATCHED
    assert state["operation_name"] == "op/1"
    assert state["execution_name"] == "exec/1"
    assert "recorded_at" in state


def test_reconcile_pending_dispatch_noop_when_nothing_pending():
    conn = FakeSettingsConnection()
    result = dispatch_state.reconcile_pending_dispatch(
        conn, "job-1", get_operation_fn=lambda name: (_ for _ in ()).throw(AssertionError("must not be called")),
    )
    assert result is None


def test_reconcile_pending_dispatch_resolves_to_confirmed_running():
    conn = FakeSettingsConnection()
    dispatch_state.record_dispatch_state(conn, "job-1", status=dispatch_state.STATUS_DISPATCH_UNKNOWN,
                                          operation_name="op/1")
    result = dispatch_state.reconcile_pending_dispatch(
        conn, "job-1", get_operation_fn=lambda name: {"name": name, "done": False},
    )
    assert result["status"] == dispatch_state.STATUS_CONFIRMED_RUNNING
    assert dispatch_state.may_dispatch(conn, "job-1") is False


def test_reconcile_pending_dispatch_resolves_to_safe_to_retry_on_operation_error():
    conn = FakeSettingsConnection()
    dispatch_state.record_dispatch_state(conn, "job-1", status=dispatch_state.STATUS_DISPATCH_UNKNOWN,
                                          operation_name="op/1")
    result = dispatch_state.reconcile_pending_dispatch(
        conn, "job-1", get_operation_fn=lambda name: {"name": name, "done": True, "error": {"code": 13}},
    )
    assert result["status"] == dispatch_state.STATUS_SAFE_TO_RETRY
    assert dispatch_state.may_dispatch(conn, "job-1") is True


def test_reconcile_pending_dispatch_stays_unknown_when_lookup_fails():
    conn = FakeSettingsConnection()
    dispatch_state.record_dispatch_state(conn, "job-1", status=dispatch_state.STATUS_DISPATCH_UNKNOWN,
                                          operation_name="op/1")

    def exploding(name):
        raise TimeoutError("deadline exceeded")

    result = dispatch_state.reconcile_pending_dispatch(conn, "job-1", get_operation_fn=exploding)
    assert result["status"] == dispatch_state.STATUS_DISPATCH_UNKNOWN
    assert dispatch_state.may_dispatch(conn, "job-1") is False


# ---- usage_budget.py ---------------------------------------------------------

def test_monthly_limits_default_when_unset():
    assert usage_budget.monthly_limits({}) == (
        usage_budget.DEFAULT_MAX_MONTHLY_EXECUTIONS, usage_budget.DEFAULT_MAX_MONTHLY_CLINICS,
    )


def test_monthly_limits_honor_env_overrides():
    env = {"CLOUD_RUN_MAX_MONTHLY_EXECUTIONS": "3", "CLOUD_RUN_MAX_MONTHLY_CLINICS": "80"}
    assert usage_budget.monthly_limits(env) == (3, 80)


def test_check_budget_allows_when_under_both_caps():
    conn = FakeSettingsConnection()
    allowed, reason = usage_budget.check_budget(conn, 50, env={"CLOUD_RUN_MAX_MONTHLY_EXECUTIONS": "2",
                                                                 "CLOUD_RUN_MAX_MONTHLY_CLINICS": "100"})
    assert allowed is True
    assert reason == ""


def test_check_budget_refuses_once_execution_count_cap_reached(monkeypatch):
    conn = FakeSettingsConnection()
    env = {"CLOUD_RUN_MAX_MONTHLY_EXECUTIONS": "1", "CLOUD_RUN_MAX_MONTHLY_CLINICS": "500"}
    usage_budget.record_dispatch_usage(conn, 10)
    allowed, reason = usage_budget.check_budget(conn, 5, env=env)
    assert allowed is False
    assert "実行回数" in reason


def test_check_budget_refuses_when_cumulative_clinics_would_exceed_cap():
    conn = FakeSettingsConnection()
    env = {"CLOUD_RUN_MAX_MONTHLY_EXECUTIONS": "20", "CLOUD_RUN_MAX_MONTHLY_CLINICS": "60"}
    usage_budget.record_dispatch_usage(conn, 50)
    allowed, reason = usage_budget.check_budget(conn, 20, env=env)
    assert allowed is False
    assert "調査件数" in reason


def test_record_dispatch_usage_accumulates_across_calls():
    conn = FakeSettingsConnection()
    usage_budget.record_dispatch_usage(conn, 25)
    usage_budget.record_dispatch_usage(conn, 15)
    usage = usage_budget.read_usage(conn)
    assert usage == {"executions": 2, "clinics": 40}


def test_usage_is_scoped_per_calendar_month():
    conn = FakeSettingsConnection()
    usage_budget.record_dispatch_usage(conn, 25, today=date(2026, 9, 30))
    usage_october = usage_budget.read_usage(conn, today=date(2026, 10, 1))
    assert usage_october == {"executions": 0, "clinics": 0}
