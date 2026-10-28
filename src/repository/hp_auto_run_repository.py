"""Persistent, transactionally fenced Auto HP controller state for Supabase.

No schema object is added.  The controller owns one JSON value in
``app_config.settings`` and serializes start/advance operations with a
transaction-scoped advisory lock plus ``SELECT ... FOR UPDATE``.  Child jobs
remain ordinary ``kind='hp'`` research jobs.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import uuid

from psycopg.types.json import Jsonb

from src.master.store import now
from src.master.hp_auto_state import initial_auto_state, advance_completed_batch
from src.repository.hp_targets import maps_hp_target_predicate


AUTO_RUN_KEY = "hp_auto_run_state"
AUTO_RUN_LOCK = "crestix:hp_auto_run:v1"
MAX_AUTO_BATCH_SIZE = 50
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

    def preflight(self, expected_role="clinic_runtime"):
        """Verify the HP Writer identity and required Run/job privileges using SELECT only."""
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT current_user,current_database(),"
                    "has_schema_privilege(current_user,'research','USAGE'),"
                    "has_schema_privilege(current_user,'app_config','USAGE'),"
                    "(has_table_privilege(current_user,'research.research_jobs','SELECT') AND "
                    "has_table_privilege(current_user,'research.research_jobs','INSERT') AND "
                    "has_table_privilege(current_user,'research.research_jobs','UPDATE')),"
                    "(has_table_privilege(current_user,'research.research_job_items','SELECT') AND "
                    "has_table_privilege(current_user,'research.research_job_items','INSERT') AND "
                    "has_table_privilege(current_user,'research.research_job_items','UPDATE')),"
                    "(has_table_privilege(current_user,'app_config.settings','SELECT') AND "
                    "has_table_privilege(current_user,'app_config.settings','UPDATE'))"
                )
                role, database, *privileges = cur.fetchone()
            self._conn.rollback()
            return {
                "ok": role == expected_role and all(privileges),
                "role": role,
                "database": database,
                "privileges_ok": all(privileges),
            }
        except Exception:
            self._conn.rollback()
            raise

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
        predicate += (
            " AND NOT EXISTS (SELECT 1 FROM research.research_job_items success_i "
            "JOIN research.research_jobs success_j ON success_j.id=success_i.job_id "
            "WHERE success_i.clinic_id=c.id AND success_i.state='DONE' "
            "AND success_i.result::text ILIKE '%%SUCCESS%%' AND success_j.kind='hp')"
        )
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
            (*args, min(MAX_AUTO_BATCH_SIZE, max(1, int(limit)))),
        )
        return [row[0] for row in cur.fetchall()]

    @staticmethod
    def _available_count(cur, prefecture, medical_types, force, auto_run_id=None):
        predicate, args = maps_hp_target_predicate(prefecture, medical_types, force)
        predicate += (
            " AND NOT EXISTS (SELECT 1 FROM research.research_job_items success_i "
            "JOIN research.research_jobs success_j ON success_j.id=success_i.job_id "
            "WHERE success_i.clinic_id=c.id AND success_i.state='DONE' "
            "AND success_i.result::text ILIKE '%%SUCCESS%%' AND success_j.kind='hp')"
        )
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
    def _insert_job(cur, clinic_ids, *, force, auto_run_id=None, extra_options=None,
                    stage_callback=None):
        jid = uuid.uuid4().hex
        options = {"force": bool(force), "max_pages": 20}
        if auto_run_id:
            options["auto_run_id"] = auto_run_id
        if extra_options:
            options.update(extra_options)
        ts = now()
        if stage_callback:
            stage_callback("insert_job_header")
        cur.execute(
            "INSERT INTO research.research_jobs(id,kind,options_json,max_searches,created_at,updated_at) "
            "VALUES(%s,'hp',%s,0,%s,%s)",
            (jid, Jsonb(options), ts, ts),
        )
        if stage_callback:
            stage_callback("insert_job_items")
        cur.executemany(
            "INSERT INTO research.research_job_items(job_id,clinic_id) VALUES(%s,%s)",
            [(jid, clinic_id) for clinic_id in clinic_ids],
        )
        return jid

    @staticmethod
    def _accumulate_completed_job_results(cur, state):
        """Fold one finished child Job into the JSON checkpoint for Reader-only progress."""
        cur.execute(
            "SELECT COALESCE(result,''),count(*) FROM research.research_job_items "
            "WHERE job_id=%s AND state='DONE' GROUP BY result",
            (state.get("current_job_id"),),
        )
        totals = {str(result): int(count) for result, count in cur.fetchall()}
        cumulative = dict(state.get("cumulative_results") or {})
        for result, count in totals.items():
            cumulative[result] = int(cumulative.get(result, 0) or 0) + count
        state["cumulative_results"] = cumulative
        # Existing checkpoints predating this field cannot account for their already
        # completed child Jobs. Keep that limitation explicit through the first resume.
        state["cumulative_history_complete"] = bool(
            state.get("cumulative_history_complete", False)
        )

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
                cur.execute(
                    "SELECT 1 FROM research.research_jobs WHERE kind='hp' AND status='RUNNING' LIMIT 1"
                )
                if cur.fetchone():
                    raise ValueError("実行中HP Jobがあります。完了または一時停止を確認してから開始してください。")
                ids = self._candidate_ids(cur, prefecture, medical_types, force, limit)
                if not ids:
                    raise ValueError("現在の条件でHP調査できる医院がありません。")
                jid = self._insert_job(cur, ids, force=force)
            self._conn.commit()
            return jid
        except Exception:
            self._conn.rollback()
            raise

    def create_error_canary_job(self, clinic_ids):
        """Atomically reserve up to ten current ERROR targets as a one-shot HP job.

        This intentionally excludes successful and Human Review rows. It does not start a
        worker; the caller must separately invoke the existing local JobRunner after the
        operator has reviewed and approved the exact candidate list.
        """
        requested = [int(value) for value in clinic_ids]
        if not requested or len(requested) > 10:
            raise ValueError("Canaryは1〜10医院にしてください。")
        if len(set(requested)) != len(requested) or any(value <= 0 for value in requested):
            raise ValueError("医院IDの重複または不正値があります。")
        try:
            with self._conn.cursor() as cur:
                self._lock(cur)
                state = self._locked_state(cur)
                if state and state.get("status") in ACTIVE_AUTO_STATUSES:
                    raise ValueError("Auto Runが未完了です。Canary Jobを作成できません。")
                cur.execute(
                    "SELECT 1 FROM research.research_jobs WHERE kind='hp' AND status='RUNNING' LIMIT 1"
                )
                if cur.fetchone():
                    raise ValueError("実行中HP Jobがあります。Canary Jobを作成できません。")
                predicate, args = maps_hp_target_predicate(force=True)
                predicate += (
                    " AND c.id=ANY(%s::bigint[]) "
                    "AND EXISTS (SELECT 1 FROM hp_research.clinic_hp_research h "
                    "WHERE h.clinic_id=c.id AND h.fetch_status='ERROR')"
                )
                cur.execute(
                    "SELECT c.id FROM public.clinics c WHERE " + predicate
                    + " ORDER BY c.id FOR UPDATE OF c SKIP LOCKED",
                    (*args, requested),
                )
                eligible = [int(row[0]) for row in cur.fetchall()]
                if set(eligible) != set(requested) or len(eligible) != len(requested):
                    raise ValueError(
                        "候補がERROR状態でない、または未完了Jobのclaim対象です。最新候補を再確認してください。"
                    )
                jid = self._insert_job(
                    cur, requested, force=True,
                    extra_options={"phase1_error_canary": True},
                )
            self._conn.commit()
            return jid
        except Exception:
            self._conn.rollback()
            raise

    def create_selected_hp_job(self, clinic_ids, research_statuses):
        """Atomically create an HP job for the exact, explicitly selected Mac targets."""
        requested = [int(value) for value in clinic_ids]
        statuses = list(dict.fromkeys(research_statuses or []))
        allowed = {"UNRESEARCHED", "ERROR", "REVIEW"}
        if not requested or len(requested) > 50:
            raise ValueError("一度に選択できる医院は1〜50件です。")
        if len(set(requested)) != len(requested) or any(value <= 0 for value in requested):
            raise ValueError("医院IDの重複または不正値があります。")
        if not statuses or any(value not in allowed for value in statuses):
            raise ValueError("SUCCESSは調査対象にできません。状態を再確認してください。")
        stage = "acquire_transaction_lock"
        def _set_write_stage(value):
            nonlocal stage
            stage = value
        try:
            with self._conn.cursor() as cur:
                self._lock(cur)
                stage = "read_autorun_state"
                state = self._locked_state(cur)
                if state and state.get("status") in ACTIVE_AUTO_STATUSES:
                    raise ValueError("Auto Runが未完了です。HP Jobを作成できません。")
                stage = "check_running_hp_job"
                cur.execute("SELECT 1 FROM research.research_jobs WHERE kind='hp' AND status='RUNNING' LIMIT 1")
                if cur.fetchone():
                    raise ValueError("実行中HP Jobがあります。完了または一時停止を確認してください。")
                predicate, args = maps_hp_target_predicate(force=True)
                predicate += (
                    " AND c.id=ANY(%s::bigint[]) AND (CASE "
                    "WHEN h.clinic_id IS NULL THEN 'UNRESEARCHED' "
                    "WHEN h.fetch_status='OK' THEN 'SUCCESS' "
                    "WHEN h.fetch_status='REVIEW' THEN 'REVIEW' ELSE 'ERROR' END)=ANY(%s::text[])"
                )
                # This column-scoped historical check belongs on the Writer
                # transaction only; Reader candidate queries must stay within
                # the granted HP projections.
                predicate += (
                    " AND NOT EXISTS (SELECT 1 FROM research.research_job_items success_i "
                    "JOIN research.research_jobs success_j ON success_j.id=success_i.job_id "
                    "WHERE success_i.clinic_id=c.id AND success_i.state='DONE' "
                    "AND success_i.result::text ILIKE '%%SUCCESS%%' AND success_j.kind='hp')"
                )
                stage = "select_and_lock_targets"
                cur.execute(
                    "SELECT c.id FROM public.clinics c LEFT JOIN hp_research.clinic_hp_research h "
                    "ON h.clinic_id=c.id WHERE " + predicate +
                    " ORDER BY c.id FOR UPDATE OF c SKIP LOCKED",
                    (*args, requested, statuses),
                )
                eligible = [int(row[0]) for row in cur.fetchall()]
                if set(eligible) != set(requested) or len(eligible) != len(requested):
                    raise ValueError("選択医院の状態が変わったか、既存Jobが予約しています。候補を再確認してください。")
                force = any(value in {"ERROR", "REVIEW"} for value in statuses)
                jid = self._insert_job(cur, requested, force=force,
                                       extra_options={"mac_explicit_statuses": statuses},
                                       stage_callback=lambda value: _set_write_stage(value))
            stage = "commit"
            self._conn.commit()
            return jid
        except Exception as exc:
            _set_write_stage(stage)
            try:
                self._conn.rollback()
            except Exception:
                pass
            diag = getattr(exc, "diag", None)
            logging.getLogger(__name__).warning(
                "HP selected Job write failed exception_type=%s sqlstate=%s stage=%s schema=%s table=%s column=%s",
                type(exc).__name__, str(getattr(exc, "sqlstate", "") or ""), stage,
                str(getattr(diag, "schema_name", "") or ""),
                str(getattr(diag, "table_name", "") or ""),
                str(getattr(diag, "column_name", "") or ""),
            )
            raise

    def start(self, prefecture, medical_types, force, batch_size):
        """Create state and Batch 1 in one transaction; concurrent starts serialize."""
        batch_size = min(MAX_AUTO_BATCH_SIZE, max(1, int(batch_size)))
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

    def mark_worker_start_failure(self, run_id, error="Worker起動を確認できませんでした。"):
        """Safely pause the just-created Run if its detached local Worker failed to start."""
        try:
            with self._conn.cursor() as cur:
                self._lock(cur)
                state = self._locked_state(cur)
                if (
                    not state
                    or state.get("run_id") != run_id
                    or state.get("status") != "RUNNING"
                    or state.get("controller_id")
                ):
                    self._conn.commit()
                    return False
                state.update({
                    "status": "ERROR",
                    "requested_pause": False,
                    "controller_id": "",
                    "controller_lease_until": "",
                    "last_error": str(error or "Worker起動を確認できませんでした。")[:200],
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
            return True
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
                # Expired owners must re-enter through acquire(). This prevents a
                # suspended process from resurrecting an expired lease while a new
                # PC is attempting takeover.
                if _parse_time(state.get("controller_lease_until")) <= db_now:
                    self._conn.commit()
                    return False
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
                self._accumulate_completed_job_results(cur, state)
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
