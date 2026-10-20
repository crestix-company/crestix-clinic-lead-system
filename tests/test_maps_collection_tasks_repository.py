"""Issue #130: tests for SupabaseMapsCollectionTasksRepository -- persistence
and transaction/locking plumbing only (the business logic itself is tested,
database-free, in test_maps_collection_tasks.py). Uses a fake Postgres
connection/cursor standing in for app_config.settings, mirroring the same
fake-connection technique already used for preview_maps_import() in
test_maps_cloud_import.py."""
import json

import pytest

from src.master.maps_collection_tasks import TaskTransitionError
from src.repository.maps_collection_tasks_repository import (
    MAPS_COLLECTION_TASKS_KEY,
    SupabaseMapsCollectionTasksRepository,
)


class _FakeCursor:
    def __init__(self, state):
        self._state = state  # mutable dict shared with the connection, like a real row
        self.queries = []

    def execute(self, sql, params=None):
        self.queries.append((sql, params))
        if "SELECT value FROM app_config.settings WHERE key=%s FOR UPDATE" in sql:
            self._last_fetch = [(json.dumps(self._state["value"]),)] if self._state["value"] is not None else []
        elif sql.startswith("SELECT value FROM app_config.settings WHERE key=%s"):
            self._last_fetch = [(json.dumps(self._state["value"]),)] if self._state["value"] is not None else []
        elif sql.startswith("INSERT INTO app_config.settings") and "DO NOTHING" in sql:
            if self._state["value"] is None:
                self._state["value"] = json.loads(params[1])
            self._last_fetch = []
        elif sql.startswith("INSERT INTO app_config.settings") and "DO UPDATE" in sql:
            self._state["value"] = json.loads(params[1])
            self._last_fetch = []
        else:
            self._last_fetch = []

    def fetchone(self):
        return self._last_fetch[0] if self._last_fetch else None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _FakeConnection:
    def __init__(self, initial=None):
        self._state = {"value": initial}
        self.committed = 0
        self.rolled_back = 0

    def cursor(self):
        return _FakeCursor(self._state)

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1


def _repo(initial=None):
    conn = _FakeConnection(initial)
    return SupabaseMapsCollectionTasksRepository(conn), conn


def test_list_tasks_returns_empty_list_when_nothing_stored_yet():
    repo, conn = _repo()
    assert repo.list_tasks() == []
    assert conn.committed == 1
    assert conn.rolled_back == 0


def test_create_task_persists_and_is_visible_via_list_tasks():
    repo, conn = _repo()
    task = repo.create_task("東京都", [1, 2, 3], timestamp="t0")
    assert task["status"] == "PENDING"
    tasks = repo.list_tasks()
    assert len(tasks) == 1
    assert tasks[0]["id"] == task["id"]
    assert conn.rolled_back == 0


def test_create_task_overlap_rejection_rolls_back_and_does_not_persist():
    repo, conn = _repo()
    repo.create_task("東京都", [1, 2], timestamp="t0")
    with pytest.raises(TaskTransitionError):
        repo.create_task("東京都", [2, 3], timestamp="t1")
    assert conn.rolled_back == 1
    # The rejected second task must never have been saved.
    tasks = repo.list_tasks()
    assert len(tasks) == 1
    assert tasks[0]["clinic_ids"] == [1, 2]


def test_assign_hold_resume_round_trip_persists_each_step():
    repo, _conn = _repo()
    task = repo.create_task("東京都", [1], timestamp="t0")
    assigned = repo.assign_task(task["id"], "PC1", timestamp="t1")
    assert assigned["status"] == "ASSIGNED"
    held = repo.hold_task(task["id"], "CAPTCHA", timestamp="t2")
    assert held["status"] == "ON_HOLD"
    resumed = repo.resume_task(task["id"])
    assert resumed["status"] == "ASSIGNED"
    assert resumed["pc_slot"] == "PC1"


def test_assign_task_pc_slot_conflict_rolls_back():
    repo, conn = _repo()
    first = repo.create_task("東京都", [1], timestamp="t0")
    second = repo.create_task("東京都", [2], timestamp="t0")
    repo.assign_task(first["id"], "PC1", timestamp="t1")
    with pytest.raises(TaskTransitionError, match="PC1"):
        repo.assign_task(second["id"], "PC1", timestamp="t1")
    assert conn.rolled_back == 1
    tasks = {t["id"]: t for t in repo.list_tasks()}
    assert tasks[second["id"]]["status"] == "PENDING"  # unaffected by the rejected attempt


def test_release_then_reassign_to_different_pc_persists():
    repo, _conn = _repo()
    task = repo.create_task("東京都", [1], timestamp="t0")
    repo.assign_task(task["id"], "PC1", timestamp="t1")
    repo.fail_task(task["id"], "操作者が中断", timestamp="t2")
    released = repo.release_task(task["id"], timestamp="t3")
    assert released["status"] == "PENDING"
    assert released["pc_slot"] is None
    reassigned = repo.assign_task(task["id"], "PC2", timestamp="t4")
    assert reassigned["pc_slot"] == "PC2"


def test_complete_task_without_overlap_rolls_back_and_task_stays_assigned():
    repo, conn = _repo()
    task = repo.create_task("東京都", [1, 2], timestamp="t0")
    repo.assign_task(task["id"], "PC1", timestamp="t1")
    with pytest.raises(TaskTransitionError):
        repo.complete_task(task["id"], imported_clinic_ids=[99], result_batch_id="key", timestamp="t2")
    assert conn.rolled_back == 1
    tasks = {t["id"]: t for t in repo.list_tasks()}
    assert tasks[task["id"]]["status"] == "ASSIGNED"


def test_complete_task_with_overlap_persists_result_batch_id():
    repo, _conn = _repo()
    task = repo.create_task("東京都", [1, 2], timestamp="t0")
    repo.assign_task(task["id"], "PC1", timestamp="t1")
    completed = repo.complete_task(task["id"], imported_clinic_ids=[2, 50], result_batch_id="abc123", timestamp="t2")
    assert completed["status"] == "COMPLETED"
    assert completed["result_batch_id"] == "abc123"
    tasks = {t["id"]: t for t in repo.list_tasks()}
    assert tasks[task["id"]]["status"] == "COMPLETED"


def test_unknown_task_id_rolls_back_rather_than_silently_succeeding():
    repo, conn = _repo()
    with pytest.raises(TaskTransitionError):
        repo.assign_task("does-not-exist", "PC1", timestamp="t1")
    assert conn.rolled_back == 1
