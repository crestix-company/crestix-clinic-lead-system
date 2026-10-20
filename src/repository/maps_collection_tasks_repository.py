"""Persistent Cloud Run collection-task tracking state for Supabase.

Mirrors src.repository.hp_auto_run_repository.SupabaseHpAutoRunRepository's
shape exactly: no new schema object is added. The task list lives as one
JSON value in the existing app_config.settings table, serialized with a
dedicated pg_advisory_xact_lock so two concurrent admin-screen requests
(e.g. two operators clicking "assign" at the same moment) cannot race each
other into an inconsistent task list -- the lock is held for the duration
of one transaction, exactly like the HP auto-run controller's own lock.

All business logic -- clinic-overlap/double-collection validation, state
transitions, the one-PC-slot invariant -- lives in
src.master.maps_collection_tasks, a pure, database-free module. This
repository is only persistence and transaction/locking plumbing: it reads
the current task list, hands it to a pure domain function, and saves
whatever that function returns (or re-raises whatever it raised, after
rolling back). That split is what makes the business logic itself testable
with synthetic data only, independent of this repository and any live
connection (Issue #130's explicit requirement).
"""
from __future__ import annotations

import json

MAPS_COLLECTION_TASKS_KEY = "maps_collection_tasks_state"
MAPS_COLLECTION_TASKS_LOCK = "crestix:maps_collection_tasks:v1"


class SupabaseMapsCollectionTasksRepository:
    def __init__(self, conn):
        self._conn = conn

    @staticmethod
    def _decode(row):
        if not row:
            return {"tasks": []}
        value = row[0]
        decoded = value if isinstance(value, dict) else json.loads(value)
        return decoded or {"tasks": []}

    @staticmethod
    def _lock(cur):
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (MAPS_COLLECTION_TASKS_LOCK,))

    def _locked_state(self, cur, *, create=False):
        if create:
            cur.execute(
                "INSERT INTO app_config.settings(key,value) VALUES(%s,%s) "
                "ON CONFLICT(key) DO NOTHING",
                (MAPS_COLLECTION_TASKS_KEY, json.dumps({"tasks": []})),
            )
        cur.execute(
            "SELECT value FROM app_config.settings WHERE key=%s FOR UPDATE",
            (MAPS_COLLECTION_TASKS_KEY,),
        )
        return self._decode(cur.fetchone())

    @staticmethod
    def _save(cur, state):
        cur.execute(
            "INSERT INTO app_config.settings(key,value) VALUES(%s,%s) "
            "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
            (MAPS_COLLECTION_TASKS_KEY, json.dumps(state, ensure_ascii=False, separators=(",", ":"))),
        )

    def list_tasks(self):
        """Read-only: no lock needed, since this never writes."""
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT value FROM app_config.settings WHERE key=%s", (MAPS_COLLECTION_TASKS_KEY,))
                state = self._decode(cur.fetchone())
            self._conn.commit()
            return state["tasks"]
        except Exception:
            self._conn.rollback()
            raise

    def _mutate(self, mutator):
        """Runs `mutator(current_tasks) -> (new_tasks, result)` under the
        advisory lock, inside one transaction. Any exception -- including a
        domain-level TaskTransitionError -- rolls back before propagating,
        so a rejected transition never leaves a partial write behind."""
        try:
            with self._conn.cursor() as cur:
                self._lock(cur)
                state = self._locked_state(cur, create=True)
                new_tasks, result = mutator(state["tasks"])
                self._save(cur, {"tasks": new_tasks})
            self._conn.commit()
            return result
        except Exception:
            self._conn.rollback()
            raise

    def create_task(self, prefecture, clinic_ids, *, timestamp):
        from src.master.maps_collection_tasks import create_task as _create_task

        def mutator(tasks):
            task = _create_task(tasks, prefecture, clinic_ids, timestamp=timestamp)
            return tasks + [task], task

        return self._mutate(mutator)

    def assign_task(self, task_id, pc_slot, *, timestamp):
        from src.master.maps_collection_tasks import assign_task as _assign_task, get_task

        def mutator(tasks):
            new_tasks = _assign_task(tasks, task_id, pc_slot, timestamp=timestamp)
            return new_tasks, get_task(new_tasks, task_id)

        return self._mutate(mutator)

    def hold_task(self, task_id, note, *, timestamp):
        from src.master.maps_collection_tasks import hold_task as _hold_task, get_task

        def mutator(tasks):
            new_tasks = _hold_task(tasks, task_id, note, timestamp=timestamp)
            return new_tasks, get_task(new_tasks, task_id)

        return self._mutate(mutator)

    def resume_task(self, task_id):
        from src.master.maps_collection_tasks import resume_task as _resume_task, get_task

        def mutator(tasks):
            new_tasks = _resume_task(tasks, task_id)
            return new_tasks, get_task(new_tasks, task_id)

        return self._mutate(mutator)

    def fail_task(self, task_id, note, *, timestamp):
        from src.master.maps_collection_tasks import fail_task as _fail_task, get_task

        def mutator(tasks):
            new_tasks = _fail_task(tasks, task_id, note, timestamp=timestamp)
            return new_tasks, get_task(new_tasks, task_id)

        return self._mutate(mutator)

    def release_task(self, task_id, *, timestamp):
        from src.master.maps_collection_tasks import release_task as _release_task, get_task

        def mutator(tasks):
            new_tasks = _release_task(tasks, task_id, timestamp=timestamp)
            return new_tasks, get_task(new_tasks, task_id)

        return self._mutate(mutator)

    def complete_task(self, task_id, imported_clinic_ids, result_batch_id, *, timestamp):
        from src.master.maps_collection_tasks import complete_task as _complete_task, get_task

        def mutator(tasks):
            new_tasks = _complete_task(
                tasks, task_id, imported_clinic_ids, result_batch_id, timestamp=timestamp,
            )
            return new_tasks, get_task(new_tasks, task_id)

        return self._mutate(mutator)
