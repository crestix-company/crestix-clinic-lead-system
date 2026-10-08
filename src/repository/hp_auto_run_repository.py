"""Persistent, transactionally fenced Auto HP controller state for Supabase.

No schema object is added.  The controller owns one JSON value in
``app_config.settings`` and serializes start/advance operations with a
transaction-scoped advisory lock plus ``SELECT ... FOR UPDATE``.  Child jobs
remain ordinary ``kind='hp'`` research jobs.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import uuid

from psycopg.types.json import Jsonb

from src.master.store import now
from src.master.hp_auto_state import initial_auto_state, advance_completed_batch
from src.repository.hp_targets import maps_hp_target_predicate


AUTO_RUN_KEY = "hp_auto_run_state"
AUTO_RUN_LOCK = "crestix:hp_auto_run:v1"
ACTIVE_AUTO_STATUSES = frozenset({"RUNNING", "WAITING_NETWORK", "PAUSED", "ERROR"})
LEASE_SECONDS = 120


def _utcnow():
    return datetime.now(timezone.utc)


def _iso(value=None):
    return (value or _utcnow()).isoformat(timespec="seconds")


def _parse_time(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)


def _db_now(cur):
    """Use the database clock so Mac/Windows clock skew cannot steal a lease."""
    cur.execute("SELECT clock_timestamp()")
    value = cur.fetchone()[0]
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class SupabaseHpAutoRunRepository:
    def __init__(self, conn):
        self._conn = conn

    @staticmethod
    def _decode(row):
        if not row:
            return None
        value = row[0]
        return value if isinstance(value, dict) else json.loads(value)

    @staticmethod
    def _lock(cur):
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (AUTO_RUN_LOCK,))

    def _locked_state(self, cur, *, create=False):
        if create:
            cur.execute(
                "INSERT INTO app_config.settings(key,value) VALUES(%s,%s) "
                "ON CONFLICT(key) DO NOTHING",
                (AUTO_RUN_KEY, json.dumps({"status": "IDLE"})),
            )
        cur.execute("SELECT value FROM app_config.settings WHERE key=%s FOR UPDATE", (AUTO_RUN_KEY,))
        return self._decode(cur.fetchone())

    @staticmethod
    def _save(cur, state):
        cur.execute(
            "INSERT INTO app_config.settings(key,value) VALUES(%s,%s) "
            "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
            (AUTO_RUN_KEY, json.dumps(state, ensure_ascii=False, separators=(",", ":"))),
        )

    @staticmethod
    def _candidate_ids(cur, prefecture, medical_types, force, limit, auto_run_id=None):
        predicate, args = maps_hp_target_predicate(prefecture, medical_types, force)
        if auto_run_id:
            predicate += (
                " AND NOT EXISTS (SELECT 1 FROM research.research_job_items prior_i "
                "JOIN research.research_jobs prior_j ON prior_j.id=prior_i.job_id "
                "WHERE prior_i.clinic_id=c.id AND prior_j.options_json->>'auto_run_id'=%s)"
            )
            args.append(auto_run_id)
        cur.execute(
            "SELECT c.id FROM public.clinics c WHERE " + predicate
            + " ORDER BY c.is_new DESC,c.id LIMIT %s FOR UPDATE OF c SKIP LOCKED",
            (*args, min(500, max(1, int(limit)))),
        )
        return [row[0] for row in cur.fetchall()]

    @staticmethod
    def _available_count(cur, prefecture, medical_types, force, auto_run_id=None):
        predicate, args = maps_hp_target_predicate(prefecture, medical_types, force)
        if auto_run_id:
            predicate += (
                " AND NOT EXISTS (SELECT 1 FROM research.research_job_items prior_i "
                "JOIN research.research_jobs prior_j ON prior_j.id=prior_i.job_id "
                "WHERE prior_i.clinic_id=c.id AND prior_j.options_json->>'auto_run_id'=%s)"
            )
            args.append(auto_run_id)
        cur.execute("SELECT count(*) FROM public.clinics c WHERE " + predicate, args)
        return int(cur.fetchone()[0])

    @staticmethod
    def _insert_job(cur, clinic_ids, *, force, auto_run_id=None):
        jid = uuid.uuid4().hex
        options = {"force": bool(force), "max_pages": 20}
        if auto_run_id:
            options["auto_run_id"] = auto_run_id
        ts = now()
        cur.execute(
            "INSERT INTO research.research_jobs(id,kind,options_json,max_searches,created_at,updated_at) "
            "VALUES(%s,'hp',%s,0,%s,%s)",
            (jid, Jsonb(options), ts, ts),
        )
        cur.executemany(
            "INSERT INTO research.research_job_items(job_id,clinic_id) VALUES(%s,%s)",
            [(jid, clinic_id) for clinic_id in clinic_ids],
        )
        return jid

    def get_state(self):
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT value FROM app_config.settings WHERE key=%s", (AUTO_RUN_KEY,))
                state = self._decode(cur.fetchone()) or {"status": "IDLE"}
            self._conn.commit()
            return state
        except Exception:
            self._conn.rollback()
            raise

    def create_single_job(self, prefecture, medical_types, force, limit):
        """Atomically reserve candidates for the existing one-shot Step 4 mode."""
        try:
            with self._conn.cursor() as cur:
                self._lock(cur)
                state = self._locked_state(cur)
                if state and state.get("status") in ACTIVE_AUTO_STATUSES:
                    raise ValueError("Auto Runが進行中です。完了または終了してから単発調査を開始してください。")
                ids = self._candidate_ids(cur, prefecture, medical_types, force, limit)
                if not ids:
                    raise ValueError("現在の条件でHP調査できる医院がありません。")
                jid = self._insert_job(cur, ids, force=force)
            self._conn.commit()
            return jid
        except Exception:
            self._conn.rollback()
            raise

    def start(self, prefecture, medical_types, force, batch_size):
        """Create state and Batch 1 in one transaction; concurrent starts serialize."""
        batch_size = min(500, max(1, int(batch_size)))
        try:
            with self._conn.cursor() as cur:
                self._lock(cur)
                previous = self._locked_state(cur, create=True)
                if previous and previous.get("status") in ACTIVE_AUTO_STATUSES:
                    raise ValueError("すでにAuto Runが進行中です。")
                cur.execute(
                    "SELECT 1 FROM research.research_jobs WHERE kind='hp' AND status='RUNNING' LIMIT 1"
                )
                if cur.fetchone():
                    raise ValueError("実行中のHP調査があります。完了または一時停止を確認してから開始してください。")
                initial_count = self._available_count(cur, prefecture, medical_types, force)
                if initial_count <= 0:
                    raise ValueError("現在の条件でHP調査できる医院がありません。")
                run_id = uuid.uuid4().hex
                ids = self._candidate_ids(cur, prefecture, medical_types, force, batch_size)
                if not ids:
                    raise ValueError("現在の条件でHP調査できる医院がありません。")
                jid = self._insert_job(cur, ids, force=force, auto_run_id=run_id)
                ts = _iso()
                state = initial_auto_state(
                    run_id=run_id, prefecture=prefecture, medical_types=medical_types,
                    force=force, batch_size=batch_size, initial_count=initial_count,
                    current_job_id=jid, timestamp=ts,
                )
                self._save(cur, state)
            self._conn.commit()
            return state
        except Exception:
            self._conn.rollback()
            raise

    def acquire(self, controller_id, lease_seconds=LEASE_SECONDS):
        try:
            with self._conn.cursor() as cur:
                self._lock(cur)
                state = self._locked_state(cur)
                if not state or state.get("status") not in {"RUNNING", "WAITING_NETWORK"}:
                    self._conn.commit()
                    return None
                owner = state.get("controller_id") or ""
                lease = _parse_time(state.get("controller_lease_until"))
                db_now = _db_now(cur)
                if owner and owner != controller_id and lease > db_now:
                    self._conn.commit()
                    return None
                state["controller_id"] = controller_id
                state["controller_lease_until"] = _iso(db_now + timedelta(seconds=lease_seconds))
                state["last_progress_at"] = _iso(db_now)
                self._save(cur, state)
            self._conn.commit()
            return state
        except Exception:
            self._conn.rollback()
            raise

    def heartbeat(self, controller_id, lease_seconds=LEASE_SECONDS):
        try:
            with self._conn.cursor() as cur:
                state = self._locked_state(cur)
                if not state or state.get("controller_id") != controller_id:
                    self._conn.commit()
                    return False
                if state.get("status") not in {"RUNNING", "WAITING_NETWORK"}:
                    self._conn.commit()
                    return False
                db_now = _db_now(cur)
                state["controller_lease_until"] = _iso(db_now + timedelta(seconds=lease_seconds))
                state["last_progress_at"] = _iso(db_now)
                self._save(cur, state)
            self._conn.commit()
            return True
        except Exception:
            self._conn.rollback()
            raise

    def advance(self, controller_id):
        """Complete the controller or create exactly one next child job atomically."""
        try:
            with self._conn.cursor() as cur:
                self._lock(cur)
                state = self._locked_state(cur)
                if not state or state.get("controller_id") != controller_id:
                    raise RuntimeError("Auto Run controller leaseを保持していません。")
                if state.get("status") != "RUNNING" or state.get("requested_pause"):
                    self._conn.commit()
                    return state
                cur.execute(
                    "SELECT status FROM research.research_jobs WHERE id=%s FOR UPDATE",
                    (state.get("current_job_id"),),
                )
                row = cur.fetchone()
                if not row or row[0] != "COMPLETED":
                    self._conn.commit()
                    return state
                remaining = self._available_count(
                    cur, state.get("prefecture", ""), state.get("medical_types"), state.get("force", False),
                    state.get("run_id"),
                )
                if remaining <= 0:
                    state = advance_completed_batch(
                        state, remaining=0, next_job_id=None, timestamp=_iso()
                    )
                    self._save(cur, state)
                    self._conn.commit()
                    return state
                ids = self._candidate_ids(
                    cur,
                    state.get("prefecture", ""),
                    state.get("medical_types"),
                    state.get("force", False),
                    state.get("batch_size", 500),
                    state.get("run_id"),
                )
                if not ids:
                    raise RuntimeError("残数がありますが次Batch候補を確保できませんでした。")
                jid = self._insert_job(
                    cur, ids, force=state.get("force", False), auto_run_id=state["run_id"]
                )
                state = advance_completed_batch(
                    state, remaining=remaining, next_job_id=jid, timestamp=_iso()
                )
                self._save(cur, state)
            self._conn.commit()
            return state
        except Exception:
            self._conn.rollback()
            raise

    def pause(self):
        try:
            with self._conn.cursor() as cur:
                self._lock(cur)
                state = self._locked_state(cur)
                if not state or state.get("status") not in {"RUNNING", "WAITING_NETWORK"}:
                    self._conn.commit()
                    return state or {"status": "IDLE"}
                state.update({
                    "status": "PAUSED",
                    "requested_pause": True,
                    "controller_id": "",
                    "controller_lease_until": "",
                    "last_progress_at": _iso(),
                })
                if state.get("current_job_id"):
                    cur.execute(
                        "UPDATE research.research_jobs SET status='PAUSED',updated_at=%s "
                        "WHERE id=%s AND status<>'COMPLETED'",
                        (now(), state["current_job_id"]),
                    )
                self._save(cur, state)
            self._conn.commit()
            return state
        except Exception:
            self._conn.rollback()
            raise

    def resume(self):
        try:
            with self._conn.cursor() as cur:
                self._lock(cur)
                state = self._locked_state(cur)
                if not state or state.get("status") not in {"PAUSED", "ERROR"}:
                    raise ValueError("再開できるAuto Runがありません。")
                state.update({
                    "status": "RUNNING",
                    "requested_pause": False,
                    "controller_id": "",
                    "controller_lease_until": "",
                    "last_error": "",
                    "last_progress_at": _iso(),
                })
                self._save(cur, state)
            self._conn.commit()
            return state
        except Exception:
            self._conn.rollback()
            raise

    def cancel(self):
        """End orchestration without deleting child jobs or research results."""
        try:
            with self._conn.cursor() as cur:
                self._lock(cur)
                state = self._locked_state(cur)
                if state and state.get("status") in ACTIVE_AUTO_STATUSES:
                    if state.get("current_job_id"):
                        cur.execute(
                            "UPDATE research.research_jobs SET status='PAUSED',updated_at=%s "
                            "WHERE id=%s AND status<>'COMPLETED'",
                            (now(), state["current_job_id"]),
                        )
                    state.update({
                        "status": "CANCELLED", "requested_pause": False,
                        "controller_id": "", "controller_lease_until": "", "last_progress_at": _iso(),
                    })
                    self._save(cur, state)
            self._conn.commit()
            return state or {"status": "IDLE"}
        except Exception:
            self._conn.rollback()
            raise

    def set_controller_status(self, controller_id, status, error="", retry_count=0):
        if status not in {"RUNNING", "WAITING_NETWORK", "ERROR"}:
            raise ValueError(status)
        try:
            with self._conn.cursor() as cur:
                state = self._locked_state(cur)
                if not state or state.get("controller_id") != controller_id:
                    self._conn.commit()
                    return False
                state.update({
                    "status": status,
                    "last_error": str(error or "")[:500],
                    "network_retry_count": int(retry_count),
                    "last_progress_at": _iso(),
                })
                if status == "ERROR":
                    state["controller_id"] = ""
                    state["controller_lease_until"] = ""
                    if state.get("current_job_id"):
                        cur.execute(
                            "UPDATE research.research_jobs SET status='PAUSED',updated_at=%s "
                            "WHERE id=%s AND status<>'COMPLETED'",
                            (now(), state["current_job_id"]),
                        )
                self._save(cur, state)
            self._conn.commit()
            return True
        except Exception:
            self._conn.rollback()
            raise

    def cumulative_results(self, run_id):
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT COALESCE(i.result,''),count(*) "
                    "FROM research.research_jobs j JOIN research.research_job_items i ON i.job_id=j.id "
                    "WHERE j.kind='hp' AND j.options_json->>'auto_run_id'=%s AND i.state='DONE' "
                    "GROUP BY i.result",
                    (run_id,),
                )
                output = {str(key): int(value) for key, value in cur.fetchall()}
            self._conn.commit()
            return output
        except Exception:
            self._conn.rollback()
            raise

    def remaining_count(self, state):
        """Candidates not yet reserved by this run (current job is added by the UI)."""
        try:
            with self._conn.cursor() as cur:
                count = self._available_count(
                    cur,
                    state.get("prefecture", ""),
                    state.get("medical_types"),
                    state.get("force", False),
                    state.get("run_id"),
                )
            self._conn.commit()
            return count
        except Exception:
            self._conn.rollback()
            raise
