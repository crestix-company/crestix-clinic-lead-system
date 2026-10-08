from datetime import datetime, timedelta, timezone
import socket

from src.master.hp_auto_state import initial_auto_state, advance_completed_batch
from src.master.store import ClinicStore
from src.repository.hp_targets import maps_hp_target_predicate
from src.repository.sqlite_write_adapter import SqliteJobsWriteRepository


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.result = None
        self.rowcount = 1
    def __enter__(self): return self
    def __exit__(self, *_args): return False
    def execute(self, query, params=None):
        self.conn.executed.append((str(query), params))
        self.result = self.conn.script.pop(0) if self.conn.script else None
    def executemany(self, query, rows):
        for params in rows:
            self.execute(query, params)
    def fetchone(self): return self.result
    def fetchall(self): return self.result or []


class _Connection:
    def __init__(self, script):
        self.script = list(script)
        self.executed = []
        self.committed = 0
        self.rolled_back = 0
    def cursor(self): return _Cursor(self)
    def commit(self): self.committed += 1
    def rollback(self): self.rolled_back += 1


def _state(total=1159, force=False):
    return initial_auto_state(
        run_id="run-1", prefecture="東京都", medical_types=["医科"], force=force,
        batch_size=500, initial_count=total, current_job_id="job-1",
        timestamp="2026-10-08T00:00:00+00:00",
    )


def test_a_b_h_500_batch_chain_1159_completes_without_extra_job():
    state = _state()
    assert state["batch_size"] == 500 and state["current_batch"] == 1
    state = advance_completed_batch(
        state, remaining=659, next_job_id="job-2", timestamp="2026-10-08T01:00:00+00:00"
    )
    assert state["status"] == "RUNNING" and state["current_batch"] == 2
    state = advance_completed_batch(
        state, remaining=159, next_job_id="job-3", timestamp="2026-10-08T02:00:00+00:00"
    )
    assert state["current_batch"] == 3 and state["completed_batches"] == 2
    state = advance_completed_batch(
        state, remaining=0, next_job_id=None, timestamp="2026-10-08T03:00:00+00:00"
    )
    assert state["status"] == "COMPLETED" and state["completed_batches"] == 3
    assert state["current_job_id"] == "job-3"  # no fourth child was created


def test_batch_size_is_always_capped_at_500():
    state = initial_auto_state(
        run_id="r", prefecture="", medical_types=["医科", "歯科"], force=False,
        batch_size=6000, initial_count=6000, current_job_id="j", timestamp="t",
    )
    assert state["batch_size"] == 500


def test_i_j_force_only_removes_ledger_exclusion_not_active_job_guard():
    normal, _ = maps_hp_target_predicate("東京都", ["医科"], force=False)
    forced, _ = maps_hp_target_predicate("東京都", ["医科"], force=True)
    assert "hp_research.clinic_hp_research" in normal
    assert "hp_research.clinic_hp_research" not in forced
    assert "queued_i.state IN ('PENDING','RUNNING')" in normal
    assert "queued_i.state IN ('PENDING','RUNNING')" in forced


def _two_item_job(tmp_path):
    store = ClinicStore(tmp_path / "auto.db")
    records = [
        {"clinic_id": f"auto-{i}", "clinic_name": f"自動医院{i}", "phone": f"03123456{i:02d}",
         "address": f"東京都千代田区{i}-1", "prefecture": "東京都", "medical_type": "医科"}
        for i in range(2)
    ]
    for record in records:
        record["as_of"] = "2026-10-01"
    store.import_master(records)
    ids = [row["id"] for row in store.query(limit=10)]
    repo = SqliteJobsWriteRepository(store)
    jid = repo.create_job(ids, "hp", {"force": False, "max_pages": 20, "auto_run_id": "run-1"}, 0)
    return store, repo, jid, ids


def test_c_d_restart_preserves_done_and_only_recovers_expired_running(tmp_path):
    store, repo, jid, ids = _two_item_job(tmp_path)
    token = repo.claim_specific_item_with_token(jid, ids[0])
    with store.connect() as conn:
        conn.execute(
            "UPDATE research_job_items SET state='DONE',result='SUCCESS' WHERE job_id=? AND clinic_id=?",
            (jid, ids[1]),
        )
    repo.recover_job_for_run(jid)
    status = repo.job_status(jid)
    assert status["counts"] == {"DONE": 1, "RUNNING": 1}
    with store.connect() as conn:
        conn.execute(
            "UPDATE research_job_items SET lease_until=? WHERE job_id=? AND clinic_id=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), jid, ids[0]),
        )
    repo.recover_job_for_run(jid)
    status = repo.job_status(jid)
    assert status["counts"] == {"DONE": 1, "PENDING": 1}
    assert token


def test_k_stale_process_cannot_finish_item_after_new_claim(tmp_path):
    store, repo, jid, ids = _two_item_job(tmp_path)
    old_token = repo.claim_specific_item_with_token(jid, ids[0])
    with store.connect() as conn:
        conn.execute(
            "UPDATE research_job_items SET lease_until=? WHERE job_id=? AND clinic_id=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), jid, ids[0]),
        )
    repo.recover_job_for_run(jid)
    new_token = repo.claim_specific_item_with_token(jid, ids[0])
    assert old_token != new_token
    assert repo.finish_claimed_item(jid, ids[0], old_token, "SUCCESS", "") is False
    assert repo.finish_claimed_item(jid, ids[0], new_token, "SUCCESS", "") is True


def test_k_supabase_start_serializes_state_and_batch_creation():
    from src.repository.hp_auto_run_repository import SupabaseHpAutoRunRepository

    ids = [(i,) for i in range(1, 501)]
    conn = _Connection([
        None, None, ('{"status":"IDLE"}',), None, (1159,), ids,
    ])
    state = SupabaseHpAutoRunRepository(conn).start("東京都", ["医科"], False, 500)
    statements = [sql for sql, _params in conn.executed]
    assert "pg_advisory_xact_lock" in statements[0]
    assert any("FOR UPDATE" in sql and "app_config.settings" in sql for sql in statements)
    assert sum("INSERT INTO research.research_job_items" in sql for sql in statements) == 500
    assert state["initial_count"] == 1159 and state["batch_size"] == 500
    assert state["current_job_id"] and conn.committed == 1 and conn.rolled_back == 0


def test_k_second_controller_cannot_start_an_active_auto_run():
    from src.repository.hp_auto_run_repository import SupabaseHpAutoRunRepository
    import pytest

    active = '{"status":"RUNNING","current_job_id":"job-1"}'
    conn = _Connection([None, None, (active,)])
    with pytest.raises(ValueError, match="すでにAuto Run"):
        SupabaseHpAutoRunRepository(conn).start("", ["医科"], False, 500)
    assert not any("INSERT INTO research.research_jobs" in sql for sql, _ in conn.executed)
    assert conn.rolled_back == 1 and conn.committed == 0


def test_k_unexpired_database_clock_lease_rejects_second_pc():
    from src.repository.hp_auto_run_repository import SupabaseHpAutoRunRepository
    import json

    db_now = datetime(2026, 10, 8, 3, 0, tzinfo=timezone.utc)
    state = {
        "status": "RUNNING", "controller_id": "mac",
        "controller_lease_until": (db_now + timedelta(seconds=60)).isoformat(),
    }
    conn = _Connection([None, (json.dumps(state),), (db_now,)])
    assert SupabaseHpAutoRunRepository(conn).acquire("windows") is None
    assert conn.committed == 1
    assert not any("ON CONFLICT(key) DO UPDATE" in sql for sql, _ in conn.executed)


def test_e_network_waiting_then_recovery_keeps_completed_work(monkeypatch):
    from src.master import hp_auto_run

    class Repo:
        def __init__(self):
            self.state = {"status": "RUNNING", "current_job_id": "job-1"}
            self.transitions = []
        def acquire(self, _controller):
            return dict(self.state) if self.state["status"] in {"RUNNING", "WAITING_NETWORK"} else None
        def set_controller_status(self, _controller, status, error="", retry_count=0):
            self.state["status"] = status
            self.transitions.append(status)
            return True
        def advance(self, _controller):
            self.state["status"] = "COMPLETED"
            return dict(self.state)
        def get_state(self):
            return dict(self.state)
        def heartbeat(self, _controller):
            return True

    repo = Repo()
    calls = {"run": 0, "done": 200}
    monkeypatch.setattr(hp_auto_run, "job_status", lambda *_: {
        "status": "COMPLETED" if calls["run"] >= 2 else "RUNNING",
        "counts": {"DONE": calls["done"], "PENDING": 300}, "total": 500,
    })
    def run(*_args, **_kwargs):
        calls["run"] += 1
        if calls["run"] == 1:
            raise ConnectionError("network is unreachable")
        calls["done"] = 500
    monkeypatch.setattr(hp_auto_run, "run_job", run)
    runner = hp_auto_run.AutoHpRunner(sleeper=lambda _seconds: None, backoffs=(0, 0, 0))
    monkeypatch.setattr(runner, "_repo", lambda _store: repo)
    runner._run(object())
    assert "WAITING_NETWORK" in repo.transitions
    assert calls["done"] == 500
    assert repo.state["status"] == "COMPLETED"


def test_e_auth_and_dns_are_not_classified_as_unbounded_network_retry():
    from src.master.hp_auto_run import classify_controller_error

    assert classify_controller_error(RuntimeError("password authentication failed")) == "fatal"
    assert classify_controller_error(socket.gaierror("name or service not known")) == "ambiguous"
    assert classify_controller_error(ConnectionError("network is unreachable")) == "temporary"


def test_f_g_pause_state_does_not_advance_until_resumed():
    state = _state()
    paused = {**state, "status": "PAUSED", "requested_pause": True}
    assert paused["current_job_id"] == state["current_job_id"]
    resumed = {**paused, "status": "RUNNING", "requested_pause": False}
    assert resumed["current_job_id"] == "job-1"
