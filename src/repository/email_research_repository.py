"""Supabase persistence and atomic queue operations for formal Email Research."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import wraps
import json
import re
import threading
import time
import unicodedata
import uuid

from psycopg.types.json import Jsonb

from src.master.email_sales_eligibility import (
    RULE_VERSION,
    AUTO_FALSE,
    AUTO_TRUE,
    REVIEW,
    classify_email_candidate,
    evaluate_sales_eligibility,
    may_auto_primary,
)


CANDIDATE_TABLE = "email_research.email_candidates"
STATUS_TABLE = "email_research.clinic_email_status"
AUTO_RUN_TABLE = "email_research.email_auto_runs"
VALID_RESEARCH_STATUSES = frozenset({
    "NOT_RESEARCHED", "RUNNING", "FOUND", "NO_EMAIL_FOUND", "REVIEW", "ERROR",
})


def _retry_closed_read_connection(method):
    """Retry one read operation after rebuilding a stale Email Research connection.

    Mirrors ``src.repository.runtime_store._retry_closed_read_connection``: the UI caches
    this repository for the process lifetime (``email_repositories_for_ui()``), so a direct
    Postgres connection can be closed by the network or server while the app stays alive.
    Reads are safe to retry once; write methods never use this decorator -- a write must
    not be silently replayed against a connection whose previous outcome is unknown.
    """
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        self._ensure_read_connection()
        try:
            return method(self, *args, **kwargs)
        except Exception as exc:
            if not self._is_retryable_connection_error(exc):
                raise
            self._reconnect_read()
            return method(self, *args, **kwargs)
    return wrapped


def utc_now():
    return datetime.now(timezone.utc)


def normalized_email(value) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()


def _json_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    return json.loads(str(value))


TERMINAL_RESEARCH_STATUSES = frozenset({"FOUND", "REVIEW", "NO_EMAIL_FOUND", "ERROR"})
EMAIL_CONTROLLER_LEASE_SECONDS = 120


def _parse_timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)


def email_controller_health(run, *, at=None):
    """Return ACTIVE/RECOVERABLE/INACTIVE from the lease stored in metrics JSON."""
    if not run or run.get("status") != "RUNNING":
        return "INACTIVE"
    metrics = _json_dict(run.get("metrics"))
    owner = str(metrics.get("controller_id") or "").strip()
    if not owner:
        return "RECOVERABLE"
    current = at or utc_now()
    return "ACTIVE" if _parse_timestamp(metrics.get("controller_lease_until")) > current else "RECOVERABLE"


class SupabaseEmailResearchRepository:
    def __init__(self, conn, *, same_database: bool = False, cohort: frozenset[int] | None = None,
                 connect_fn=None):
        self._conn = conn
        # same_database=True: email_research.clinic_targets is a live view over the
        # Clinic/HP source tables (see email_research_schema.sql's same_database block),
        # not a physically-synced snapshot table. claim_next() must not request a row lock
        # on it (Postgres refuses FOR UPDATE on a non-simple multi-table view) and
        # sync_targets() has nothing of its own left to write into it -- eligibility is
        # already live on every read.
        self._same_database = bool(same_database)
        # Canary mode (cohort is not None): sync/claim/write are all restricted to this
        # exact clinic_id set, never to the live Production-wide eligible pool. None
        # (the default) is the normal, continuous Auto Run path -- every cohort check
        # below is skipped entirely and behavior is unchanged from before this existed.
        self._cohort = frozenset(cohort) if cohort is not None else None
        # Injectable only for tests; production always lazily imports the real connector
        # so a fake connection factory never leaks into the default code path.
        self._connect_fn = connect_fn
        # Multiple Streamlit sessions can share this process-cached repository and trigger
        # concurrent fragment reruns; only one of them may rebuild self._conn at a time.
        self._reconnect_lock = threading.Lock()

    def _check_cohort(self, clinic_id: int) -> None:
        if self._cohort is not None and int(clinic_id) not in self._cohort:
            raise ValueError(f"Canary cohort外のclinic_id({clinic_id})への書き込みを拒否しました。")

    def _connection_is_dead(self) -> bool:
        return bool(getattr(self._conn, "closed", False) or getattr(self._conn, "broken", False))

    def _is_retryable_connection_error(self, exc) -> bool:
        try:
            import psycopg
        except ImportError:
            return False
        return isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError))

    def _safe_rollback(self) -> None:
        """Never let a rollback on an already-dead connection mask the real error."""
        try:
            if not getattr(self._conn, "closed", False):
                self._conn.rollback()
        except Exception:
            pass

    def close(self) -> None:
        """Close this factory-owned connection; callers decide repository lifetime."""
        try:
            if not getattr(self._conn, "closed", False):
                self._conn.close()
        except Exception:
            pass

    def _reconnect_read(self) -> None:
        """Rebuild self._conn from the configured Email Research DB URL.

        Secrets are never logged: the URL is read once from the environment, handed
        straight to the connector, and never captured in an exception message or log line.
        """
        with self._reconnect_lock:
            if not self._connection_is_dead():
                return
            # Deliberately never call old_conn.close() here: another thread may still
            # hold its own local reference to this same object mid-query (Streamlit
            # fragments on independent run_every timers share this repository). Closing
            # it out from under that thread would turn a transient disconnect into a
            # second, self-inflicted one. It is already dead/unusable either way, so it
            # is simply dropped and left for garbage collection once nothing references it.
            import os
            from src.repository.email_database import EMAIL_RESEARCH_DB_URL_ENV_VAR
            url = (os.environ.get(EMAIL_RESEARCH_DB_URL_ENV_VAR) or "").strip()
            if not url:
                raise RuntimeError("Email Research DB接続情報(環境変数)が見つからないため再接続できません。")
            connect_fn = self._connect_fn
            if connect_fn is None:
                from src.repository.supabase_adapter import connect as connect_fn
            new_conn = connect_fn(url, autocommit=False)
            with new_conn.cursor() as cur:
                cur.execute("SELECT 1")
            new_conn.commit()
            self._conn = new_conn

    def _ensure_read_connection(self) -> None:
        if self._connection_is_dead():
            self._reconnect_read()

    def available(self) -> bool:
        self._ensure_read_connection()
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT to_regclass(%s),to_regclass(%s),to_regclass(%s)",
                    (CANDIDATE_TABLE, STATUS_TABLE, AUTO_RUN_TABLE),
                )
                row = cur.fetchone()
            self._conn.commit()
            return bool(row and all(value is not None for value in row))
        except Exception as exc:
            self._safe_rollback()
            if not self._is_retryable_connection_error(exc):
                raise
            try:
                self._reconnect_read()
            except Exception:
                return False
            try:
                with self._conn.cursor() as cur:
                    cur.execute(
                        "SELECT to_regclass(%s),to_regclass(%s),to_regclass(%s)",
                        (CANDIDATE_TABLE, STATUS_TABLE, AUTO_RUN_TABLE),
                    )
                    row = cur.fetchone()
                self._conn.commit()
                return bool(row and all(value is not None for value in row))
            except Exception:
                self._safe_rollback()
                return False

    @staticmethod
    def _candidate_payload(candidate: dict, *, research_run_id: str = "") -> dict:
        email = str(candidate.get("email") or "").strip()
        normalized = normalized_email(email)
        if not email or "@" not in normalized:
            raise ValueError("Email Candidateのメールアドレスを確認してください。")
        result = evaluate_sales_eligibility(
            auto_identity_decision=candidate.get("auto_identity_decision"),
            role=candidate.get("role"),
            ownership=candidate.get("email_ownership") or candidate.get("ownership"),
            clinic_name_match=candidate.get("clinic_name_match"),
            prefecture_match=candidate.get("prefecture_match"),
            city_match=candidate.get("city_match"),
            discovery_source=candidate.get("discovery_source", ""),
            confidence=candidate.get("confidence", ""),
            extra_hard_block_codes=candidate.get("hard_block_codes") or (),
        )
        classification = classify_email_candidate({**candidate,
            "clinic_name_match": candidate.get("clinic_name_match"),
            "prefecture_match": candidate.get("prefecture_match"),
            "city_match": candidate.get("city_match"),
        })
        disposition = classification.auto_decision
        snapshot = dict(candidate.get("auto_snapshot") or {})
        snapshot.update({
            "eligibility_rule_version": result.rule_version,
            "confidence": classification.confidence,
            "confidence_evidence": candidate.get("confidence_evidence") or {},
        })
        researched_at = candidate.get("researched_at") or utc_now()
        return {
            "clinic_id": int(candidate["clinic_id"]),
            "research_run_id": str(research_run_id or candidate.get("research_run_id") or ""),
            "email": email,
            "normalized_email": normalized,
            "discovery_source": str(candidate.get("discovery_source") or "SEARCH_QUERY").upper(),
            "source_url": str(candidate.get("source_url") or ""),
            "search_query": str(candidate.get("search_query") or ""),
            "role": result.role,
            "ownership": result.ownership,
            "email_ownership": result.ownership,
            "source_owner_type": str(candidate.get("source_owner_type") or "UNKNOWN").upper(),
            "verification_status": str(candidate.get("verification_status") or "REVIEW"),
            "auto_decision": disposition,
            "auto_identity_decision": classification.auto_identity_decision,
            "confidence": result.confidence,
            "clinic_name_match": candidate.get("clinic_name_match"),
            "prefecture_match": candidate.get("prefecture_match"),
            "city_match": candidate.get("city_match"),
            "verification_reason": str(candidate.get("verification_reason") or ""),
            "rejection_reason": str(candidate.get("rejection_reason") or ""),
            "sales_usable": classification.sales_usable,
            "hard_block_codes": list(classification.hard_block_codes),
            "final_sales_eligible": classification.final_sales_eligible,
            "auto_snapshot": snapshot,
            "rule_version": str(candidate.get("rule_version") or RULE_VERSION),
            "researched_at": researched_at,
        }

    @staticmethod
    def _upsert_candidate_tx(cur, payload: dict) -> int:
        cur.execute(
            """
            INSERT INTO email_research.email_candidates(
              clinic_id,research_run_id,email,normalized_email,discovery_source,source_url,
              search_query,role,ownership,email_ownership,source_owner_type,verification_status,
              auto_decision,auto_identity_decision,
              confidence,clinic_name_match,prefecture_match,city_match,verification_reason,
              rejection_reason,sales_usable,hard_block_codes,final_sales_eligible,
              auto_snapshot,rule_version,researched_at,created_at,updated_at
            ) VALUES(
              %(clinic_id)s,%(research_run_id)s,%(email)s,%(normalized_email)s,
              %(discovery_source)s,%(source_url)s,%(search_query)s,%(role)s,%(ownership)s,
              %(email_ownership)s,%(source_owner_type)s,%(verification_status)s,
              %(auto_decision)s,%(auto_identity_decision)s,%(confidence)s,
              %(clinic_name_match)s,%(prefecture_match)s,%(city_match)s,
              %(verification_reason)s,%(rejection_reason)s,%(sales_usable)s,
              %(hard_block_codes)s,%(final_sales_eligible)s,%(auto_snapshot)s,
              %(rule_version)s,%(researched_at)s,now(),now()
            )
            ON CONFLICT(clinic_id,normalized_email) DO UPDATE SET
              research_run_id=EXCLUDED.research_run_id,
              email=EXCLUDED.email,
              discovery_source=EXCLUDED.discovery_source,
              source_url=EXCLUDED.source_url,
              search_query=EXCLUDED.search_query,
              role=EXCLUDED.role,
              ownership=EXCLUDED.ownership,
              email_ownership=EXCLUDED.email_ownership,
              source_owner_type=EXCLUDED.source_owner_type,
              verification_status=EXCLUDED.verification_status,
              auto_decision=EXCLUDED.auto_decision,
              auto_identity_decision=EXCLUDED.auto_identity_decision,
              confidence=EXCLUDED.confidence,
              clinic_name_match=EXCLUDED.clinic_name_match,
              prefecture_match=EXCLUDED.prefecture_match,
              city_match=EXCLUDED.city_match,
              verification_reason=EXCLUDED.verification_reason,
              rejection_reason=EXCLUDED.rejection_reason,
              sales_usable=EXCLUDED.sales_usable,
              hard_block_codes=EXCLUDED.hard_block_codes,
              final_sales_eligible=EXCLUDED.final_sales_eligible,
              auto_snapshot=EXCLUDED.auto_snapshot,
              rule_version=EXCLUDED.rule_version,
              researched_at=EXCLUDED.researched_at,
              updated_at=now()
            RETURNING candidate_id
            """,
            {**payload, "auto_snapshot": Jsonb(payload["auto_snapshot"])},
        )
        return int(cur.fetchone()[0])

    def upsert_candidate(self, candidate: dict, *, research_run_id: str = "") -> int:
        payload = self._candidate_payload(candidate, research_run_id=research_run_id)
        self._check_cohort(payload["clinic_id"])
        try:
            with self._conn.cursor() as cur:
                candidate_id = self._upsert_candidate_tx(cur, payload)
            self._conn.commit()
            return candidate_id
        except Exception:
            self._safe_rollback()
            raise

    @_retry_closed_read_connection
    def candidate(self, candidate_id: int) -> dict | None:
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM email_research.email_candidates WHERE candidate_id=%s",
                    (int(candidate_id),),
                )
                row = cur.fetchone()
                if not row:
                    result = None
                else:
                    columns = [item.name for item in cur.description]
                    result = dict(zip(columns, row))
            self._conn.commit()
            return result
        except Exception:
            self._safe_rollback()
            raise

    def sync_targets(self, targets, *, limit: int | None = None) -> int:
        """Sync a verified-HP snapshot supplied by the separate read-only target reader.

        In same_database mode clinic_targets is a live view, so there is nothing to
        write into it; the `targets` argument is accepted (and ignored) purely so
        EmailAutoRunner's single sync_targets() call site works unchanged in both modes.
        The only remaining job is seeding clinic_email_status rows for clinics that just
        became eligible, read straight off the same live view.
        """
        if self._same_database:
            return self._sync_targets_same_database()
        rows = list(targets or ())
        if self._cohort is not None:
            # Canary mode: the target_reader's own result is not trusted as the scope
            # boundary -- only cohort clinic_ids are ever synced, regardless of what the
            # reader returned.
            rows = [row for row in rows if int(row["clinic_id"]) in self._cohort]
        if limit is not None:
            rows = rows[:max(0, int(limit))]
        clinic_ids = []
        try:
            with self._conn.cursor() as cur:
                count = 0
                for target in rows:
                    clinic_id = int(target["clinic_id"])
                    clinic_uuid = str(target.get("clinic_uuid") or "").strip()
                    hp_url = str(target.get("official_hp_url") or target.get("hp_url") or "").strip()
                    if clinic_id <= 0 or not clinic_uuid or not hp_url:
                        raise ValueError("Email targetにはstable clinic_id、clinic_uuid、確認済みHP URLが必要です。")
                    if clinic_id in clinic_ids:
                        raise ValueError("Target Readerにclinic_id重複があります。")
                    clinic_ids.append(clinic_id)
                    # Insert missing queue state before locking/updating the target row.
                    # This avoids a status->target / target->status lock-order cycle with claim.
                    cur.execute(
                        "INSERT INTO email_research.clinic_email_status(clinic_id) VALUES(%s) "
                        "ON CONFLICT(clinic_id) DO NOTHING", (clinic_id,),
                    )
                    cur.execute(
                        """
                        INSERT INTO email_research.clinic_targets(
                          clinic_id,clinic_uuid,clinic_name,prefecture,city,address,phone,
                          official_hp_url,hp_verified_at,synced_at
                        ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                        ON CONFLICT(clinic_id) DO UPDATE SET
                          clinic_uuid=EXCLUDED.clinic_uuid,clinic_name=EXCLUDED.clinic_name,
                          prefecture=EXCLUDED.prefecture,city=EXCLUDED.city,address=EXCLUDED.address,
                          phone=EXCLUDED.phone,official_hp_url=EXCLUDED.official_hp_url,
                          hp_verified_at=EXCLUDED.hp_verified_at,eligible=true,synced_at=now()
                        WHERE email_research.clinic_targets.clinic_uuid=EXCLUDED.clinic_uuid
                          AND (email_research.clinic_targets.eligible IS DISTINCT FROM true
                            OR email_research.clinic_targets.clinic_name IS DISTINCT FROM EXCLUDED.clinic_name
                            OR email_research.clinic_targets.prefecture IS DISTINCT FROM EXCLUDED.prefecture
                            OR email_research.clinic_targets.city IS DISTINCT FROM EXCLUDED.city
                            OR email_research.clinic_targets.address IS DISTINCT FROM EXCLUDED.address
                            OR email_research.clinic_targets.phone IS DISTINCT FROM EXCLUDED.phone
                            OR email_research.clinic_targets.official_hp_url IS DISTINCT FROM EXCLUDED.official_hp_url
                            OR email_research.clinic_targets.hp_verified_at IS DISTINCT FROM EXCLUDED.hp_verified_at)
                        """,
                        (clinic_id,clinic_uuid,str(target.get("clinic_name") or ""),
                         str(target.get("prefecture") or ""),str(target.get("city") or ""),
                         str(target.get("address") or ""),str(target.get("phone") or ""),
                         hp_url,target.get("hp_verified_at")),
                    )
                    if cur.rowcount != 1:
                        cur.execute(
                            "SELECT clinic_uuid FROM email_research.clinic_targets WHERE clinic_id=%s",
                            (clinic_id,),
                        )
                        existing = cur.fetchone()
                        if not existing or str(existing[0]) != clinic_uuid:
                            raise ValueError("既存clinic_idとclinic_uuidの対応が一致しません。")
                    count += 1
                if self._cohort is None:
                    # Full-sync staleness sweep: never run in Canary mode -- clinic_ids
                    # here only ever contains the cohort, and this clinic_targets table
                    # may be shared with the normal, full-pool Auto Run. Marking every
                    # non-cohort clinic ineligible would corrupt that shared snapshot.
                    cur.execute(
                        "UPDATE email_research.clinic_targets SET eligible=false,synced_at=now() "
                        "WHERE eligible=true AND NOT (clinic_id=ANY(%s::bigint[]))", (clinic_ids,),
                    )
            self._conn.commit()
            return int(count or 0)
        except Exception:
            self._safe_rollback()
            raise

    def _sync_targets_same_database(self) -> int:
        try:
            with self._conn.cursor() as cur:
                if self._cohort is not None:
                    # Canary mode: intersect the live eligible view with the fixed
                    # cohort -- never seed a clinic_id outside it, no matter how large
                    # the live Production-wide eligible pool is.
                    cur.execute(
                        """
                        INSERT INTO email_research.clinic_email_status(clinic_id)
                        SELECT clinic_id FROM email_research.clinic_targets
                        WHERE eligible=true AND clinic_id=ANY(%s::bigint[])
                        ON CONFLICT(clinic_id) DO NOTHING
                        """,
                        (list(self._cohort),),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO email_research.clinic_email_status(clinic_id)
                        SELECT clinic_id FROM email_research.clinic_targets WHERE eligible=true
                        ON CONFLICT(clinic_id) DO NOTHING
                        """
                    )
                count = cur.rowcount
            self._conn.commit()
            return int(count or 0)
        except Exception:
            self._safe_rollback()
            raise

    def claim_next(self, *, worker_id: str, run_id: str, lease_seconds: int = 300) -> dict | None:
        worker_id = str(worker_id or "").strip()
        if not worker_id:
            raise ValueError("Email worker_idを指定してください。")
        claim_token = uuid.uuid4().hex
        try:
            with self._conn.cursor() as cur:
                # The shared run-row lock serializes Pause/Stop against the very short
                # claim transaction without serializing workers with each other. After
                # a control action returns, no worker can claim from the old RUNNING state.
                cur.execute(
                    """
                    SELECT run_id FROM email_research.email_auto_runs
                    WHERE run_id=%s AND status='RUNNING' AND stop_requested=false
                    FOR SHARE
                    """, (str(run_id or ""),),
                )
                if not cur.fetchone():
                    self._conn.commit()
                    return None
                cur.execute(
                    """
                    UPDATE email_research.clinic_email_status
                    SET research_status='NOT_RESEARCHED',claim_token='',claimed_by='',
                        claim_expires_at=NULL,updated_at=now(),last_error=''
                    WHERE research_status='RUNNING' AND claim_expires_at<now()
                    """
                )
                # clinic_targets is a physical table in cross_database mode (lockable) and
                # a live view over the Clinic/HP source in same_database mode (Postgres
                # refuses FOR UPDATE naming a non-simple multi-table view). Locking clinic_targets
                # in cross_database mode is what makes this claim consistent with a
                # concurrent sync_targets() marking the same clinic ineligible; in
                # same_database mode there is no separate sync step to race against --
                # eligibility is read live in the same statement -- so locking only the
                # clinic_email_status row being claimed is both correct and necessary.
                lock_clause = "FOR UPDATE OF s SKIP LOCKED" if self._same_database else "FOR UPDATE OF s,c SKIP LOCKED"
                # Canary mode: never claim outside the fixed cohort, regardless of what
                # sync_targets() seeded (defense in depth -- sync is already cohort-scoped,
                # but claim does not trust that alone).
                cohort_clause = "AND s.clinic_id=ANY(%s::bigint[])\n" if self._cohort is not None else ""
                cohort_params = (list(self._cohort),) if self._cohort is not None else ()
                cur.execute(
                    """
                    WITH next_row AS (
                      SELECT s.clinic_id
                      FROM email_research.clinic_email_status s
                      JOIN email_research.clinic_targets c ON c.clinic_id=s.clinic_id
                      WHERE s.research_status='NOT_RESEARCHED'
                        AND s.primary_email_candidate_id IS NULL
                        AND c.eligible=true
                        AND COALESCE(BTRIM(c.official_hp_url),'')<>''
                        AND c.manual_exclude=false
                        """ + cohort_clause + """
                      ORDER BY s.updated_at,s.clinic_id
                      LIMIT 1 """ + lock_clause + """
                    )
                    UPDATE email_research.clinic_email_status s
                    SET research_status='RUNNING',current_run_id=%s,claim_token=%s,
                        claimed_by=%s,claim_expires_at=now()+(%s * interval '1 second'),
                        started_at=now(),updated_at=now(),attempts=s.attempts+1,last_error=''
                    FROM next_row n WHERE s.clinic_id=n.clinic_id
                    RETURNING s.clinic_id,s.claim_token,s.attempts
                    """,
                    cohort_params + (str(run_id or ""), claim_token, worker_id, max(30, int(lease_seconds))),
                )
                claimed = cur.fetchone()
                if not claimed:
                    result = None
                else:
                    cur.execute(
                        """
                        SELECT c.clinic_id,c.clinic_uuid,c.clinic_name,c.prefecture,c.city,
                          c.address,c.phone,c.official_hp_url,c.hp_verified_at
                        FROM email_research.clinic_targets c WHERE c.clinic_id=%s
                        """,
                        (claimed[0],),
                    )
                    clinic = cur.fetchone()
                    result = {
                        "clinic_id": clinic[0], "clinic_uuid": clinic[1], "clinic_name": clinic[2],
                        "prefecture": clinic[3], "city": clinic[4],"address": clinic[5], "phone": clinic[6],
                        "hp_url": clinic[7], "official_hp_url": clinic[7], "hp_verified_at": clinic[8],
                        "claim_token": claimed[1], "attempts": claimed[2],
                    }
            self._conn.commit()
            return result
        except Exception:
            self._safe_rollback()
            raise

    def renew_claim(self, *, clinic_id: int, claim_token: str, lease_seconds: int = 300) -> bool:
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE email_research.clinic_email_status
                    SET claim_expires_at=now()+(%s * interval '1 second'),updated_at=now()
                    WHERE clinic_id=%s AND research_status='RUNNING' AND claim_token=%s
                    """,
                    (max(30, int(lease_seconds)), int(clinic_id), str(claim_token)),
                )
                updated = cur.rowcount == 1
            self._conn.commit()
            return updated
        except Exception:
            self._safe_rollback()
            raise

    def complete_claim(
        self, *, clinic_id: int, claim_token: str, candidates: list[dict],
        research_status: str, metrics: dict | None = None, error: str = "",
        research_run_id: str = "", primary_minimum_confidence: str | None = None,
        rejection_evidence: list[dict] | None = None,
    ) -> dict:
        db_write_started = time.perf_counter()
        self._check_cohort(clinic_id)
        if research_status not in VALID_RESEARCH_STATUSES - {"NOT_RESEARCHED", "RUNNING"}:
            raise ValueError("Email Researchの完了状態を確認してください。")
        all_payloads = [self._candidate_payload({**item, "clinic_id": int(clinic_id)}, research_run_id=research_run_id)
                        for item in candidates]
        payloads = [payload for payload in all_payloads if payload["auto_decision"] in {AUTO_TRUE, REVIEW}]
        rejections = [payload for payload in all_payloads if payload["auto_decision"] == AUTO_FALSE]
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT claim_token FROM email_research.clinic_email_status
                    WHERE clinic_id=%s AND research_status='RUNNING' FOR UPDATE
                    """, (int(clinic_id),),
                )
                current = cur.fetchone()
                if not current or str(current[0]) != str(claim_token):
                    raise ValueError("Email Research claimが失効しています。")
                ids = [self._upsert_candidate_tx(cur, payload) for payload in payloads]
                for rejection in [*rejections, *(rejection_evidence or [])]:
                    cur.execute(
                        """
                        INSERT INTO email_research.email_rejection_evidence(
                          clinic_id,research_run_id,normalized_email,source_url,source_type,
                          auto_decision,rejection_reason,hard_block_codes,rejected_at,auto_snapshot
                        ) VALUES(%s,%s,%s,%s,%s,'AUTO_FALSE',%s,%s,now(),%s)
                        """,
                        (int(clinic_id), str(research_run_id or ""), rejection["normalized_email"],
                         rejection.get("source_url", ""), rejection.get("source_type", rejection.get("discovery_source", "")),
                         rejection.get("rejection_reason", "") or "hard block", rejection.get("hard_block_codes", []),
                         Jsonb(rejection.get("auto_snapshot", {}))),
                    )
                primary_id = None
                eligible = [
                    (candidate_id, payload) for candidate_id, payload in zip(ids, payloads)
                    if payload["auto_decision"] == AUTO_TRUE
                    and may_auto_primary(payload, minimum_confidence=primary_minimum_confidence)
                ]
                if eligible:
                    source_rank = {"OFFICIAL_HP": 0, "NAVII": 1, "SEARCH_QUERY": 2, "AI_OVERVIEW": 3}
                    confidence_rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
                    role_rank = {"GENERAL": 0, "OFFICE": 1}
                    primary_id, _ = min(eligible, key=lambda item: (
                        source_rank.get(item[1]["discovery_source"], 9),
                        role_rank.get(item[1]["role"], 9),
                        confidence_rank.get(item[1]["confidence"], 9),
                        item[1]["normalized_email"],
                    ))
                cur.execute(
                    """
                    UPDATE email_research.clinic_email_status
                    SET research_status=%s,candidate_count=%s,primary_email_candidate_id=%s,
                        rule_version=%s,last_error=%s,researched_at=now(),updated_at=now(),
                        claim_token='',claimed_by='',claim_expires_at=NULL,metrics=%s
                    WHERE clinic_id=%s AND claim_token=%s
                    """,
                    (research_status, len(ids), primary_id, RULE_VERSION, str(error or "")[:4000],
                     Jsonb({**(metrics or {}), "db_write_ms": (time.perf_counter() - db_write_started) * 1000,
                            "candidate_found_count": (metrics or {}).get("candidate_found_count", len(all_payloads)),
                            "valid_candidate_count": len(payloads),
                            "auto_false_count": len(rejections) + len(rejection_evidence or [])}),
                     int(clinic_id), str(claim_token)),
                )
                if cur.rowcount != 1:
                    raise ValueError("Email Research claimの更新に失敗しました。")
            self._conn.commit()
            return {"candidate_ids": ids, "primary_email_candidate_id": primary_id}
        except Exception:
            self._safe_rollback()
            raise

    def fail_claim(self, *, clinic_id: int, claim_token: str, error: str, metrics: dict | None = None):
        return self.complete_claim(
            clinic_id=clinic_id, claim_token=claim_token, candidates=[], research_status="ERROR",
            error=error, metrics=metrics,
        )

    @_retry_closed_read_connection
    def cohort_pending_count(self) -> int | None:
        """None outside Canary mode (self._cohort is None) -- EmailAutoRunner uses that
        to distinguish "not a Canary run" from "Canary run, 0 remaining". Otherwise the
        count of cohort clinic_ids that have not yet reached a terminal research_status
        (including any not seeded into clinic_email_status at all yet, which counts as
        pending, not terminal)."""
        if self._cohort is None:
            return None
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT count(*) FROM email_research.clinic_email_status
                    WHERE clinic_id=ANY(%s::bigint[]) AND research_status=ANY(%s::text[])
                    """,
                    (list(self._cohort), list(TERMINAL_RESEARCH_STATUSES)),
                )
                terminal_count = int(cur.fetchone()[0] or 0)
            self._conn.commit()
            return len(self._cohort) - terminal_count
        except Exception:
            self._safe_rollback()
            raise

    @_retry_closed_read_connection
    def cohort_batch_summary(self) -> dict | None:
        """Cohort-scoped terminal breakdown for the Email Research UI's batch progress
        card. None outside Canary mode (self._cohort is None), distinct from summary()
        which is an all-time, non-cohort-scoped total across every run ever started."""
        if self._cohort is None:
            return None
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT count(*) FILTER(WHERE research_status='FOUND'),
                      count(*) FILTER(WHERE research_status='REVIEW'),
                      count(*) FILTER(WHERE research_status='NO_EMAIL_FOUND'),
                      count(*) FILTER(WHERE research_status='ERROR'),
                      count(*) FILTER(WHERE research_status=ANY(%s::text[]))
                    FROM email_research.clinic_email_status
                    WHERE clinic_id=ANY(%s::bigint[])
                    """,
                    (list(TERMINAL_RESEARCH_STATUSES), list(self._cohort)),
                )
                found, review, no_email, error, terminal = cur.fetchone()
            self._conn.commit()
            cohort_size = len(self._cohort)
            return {
                "cohort_size": cohort_size, "processed": int(terminal or 0),
                "pending": cohort_size - int(terminal or 0),
                "found": int(found or 0), "review": int(review or 0),
                "no_email_found": int(no_email or 0), "error": int(error or 0),
            }
        except Exception:
            self._safe_rollback()
            raise

    @_retry_closed_read_connection
    def status_for_ids(self, clinic_ids: list[int]) -> dict[int, dict]:
        """One bulk lookup for a Clinic listing page's Email Status column -- never
        call per-row; mirrors the existing treatment_status_for_ids bulk pattern."""
        ids = [int(cid) for cid in clinic_ids]
        if not ids or not self.available():
            return {}
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT clinic_id, research_status, primary_email_candidate_id IS NOT NULL AS has_primary
                    FROM email_research.clinic_email_status
                    WHERE clinic_id=ANY(%s::bigint[])
                    """, (ids,),
                )
                rows = cur.fetchall()
            self._conn.commit()
            return {int(row[0]): {"research_status": row[1], "has_primary": bool(row[2])} for row in rows}
        except Exception:
            self._safe_rollback()
            raise

    @_retry_closed_read_connection
    def primary_listing(self, *, limit: int = 500) -> list[dict]:
        """Clinic-unit listing of usable Primary Emails only (1 row per Clinic, the
        guard-selected primary_email_candidate_id -- never a raw Candidate count)."""
        if not self.available():
            return []
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT s.clinic_id, t.clinic_name, t.prefecture, t.city,
                      p.email, p.role, p.ownership, p.email_ownership, p.discovery_source,
                      p.source_owner_type, p.source_url, p.confidence, p.hard_block_codes,
                      p.final_sales_eligible, s.updated_at
                    FROM email_research.clinic_email_status s
                    JOIN email_research.email_candidates p
                      ON p.clinic_id=s.clinic_id AND p.candidate_id=s.primary_email_candidate_id
                    LEFT JOIN email_research.clinic_targets t ON t.clinic_id=s.clinic_id
                    WHERE s.primary_email_candidate_id IS NOT NULL
                    ORDER BY s.updated_at DESC
                    LIMIT %s
                    """, (int(limit),),
                )
                rows = cur.fetchall()
                columns = [item.name for item in cur.description]
            self._conn.commit()
            return [dict(zip(columns, row)) for row in rows]
        except Exception:
            self._safe_rollback()
            raise

    PRIMARY_LISTING_FILTER_VALUES = {
        "research_status": ("NOT_RESEARCHED", "RUNNING", "FOUND", "NO_EMAIL_FOUND", "REVIEW", "ERROR"),
        "auto_decision": ("AUTO_TRUE", "AUTO_FALSE", "REVIEW"),
        "confidence": ("HIGH", "MEDIUM", "LOW"),
        "role": ("GENERAL", "OFFICE", "RESERVATION", "RECRUIT", "RECRUITING",
                 "MEDICAL_COOPERATION", "PERSONAL", "UNKNOWN"),
        "ownership": ("CLINIC_OWNED", "SAME_MEDICAL_CORPORATION", "THIRD_PARTY_OWNED", "OWNERSHIP_UNKNOWN"),
    }

    @classmethod
    def _primary_listing_where(cls, filters: dict | None) -> tuple[str, list]:
        """SQL WHERE for the Clinic-unit Email listing. Every filter runs in PostgreSQL with bound
        parameters; list filters accept only the schema CHECK values (anything else is dropped)."""
        filters = dict(filters or {})
        clauses, args = [], []
        has_email = filters.get("has_email", "yes")
        if has_email == "yes":
            clauses.append("s.primary_email_candidate_id IS NOT NULL")
        elif has_email == "no":
            clauses.append("s.primary_email_candidate_id IS NULL")
        prefecture = str(filters.get("prefecture") or "").strip()
        if prefecture:
            clauses.append("t.prefecture=%s"); args.append(prefecture)
        clinic_name = str(filters.get("clinic_name") or "").strip()
        if clinic_name:
            escaped = clinic_name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("t.clinic_name ILIKE %s"); args.append(f"%{escaped}%")
        for key, column in (("research_status", "s.research_status"), ("confidence", "p.confidence"),
                            ("role", "p.role"), ("ownership", "p.ownership")):
            values = [v for v in (filters.get(key) or []) if v in cls.PRIMARY_LISTING_FILTER_VALUES[key]]
            if values:
                clauses.append(f"{column}=ANY(%s::text[])"); args.append(values)
        decisions = [v for v in (filters.get("auto_decision") or [])
                     if v in cls.PRIMARY_LISTING_FILTER_VALUES["auto_decision"]]
        if decisions:
            # Clinic has at least one Candidate with that AI decision (Primary or not). Rejected
            # emails are persisted in email_rejection_evidence (always AUTO_FALSE), not as Candidates.
            decision_sql = ("EXISTS (SELECT 1 FROM email_research.email_candidates d "
                            "WHERE d.clinic_id=s.clinic_id AND d.auto_decision=ANY(%s::text[]))")
            args.append(decisions)
            if "AUTO_FALSE" in decisions:
                decision_sql = ("(" + decision_sql + " OR EXISTS (SELECT 1 FROM email_research.email_rejection_evidence r "
                                "WHERE r.clinic_id=s.clinic_id))")
            clauses.append(decision_sql)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", args

    def primary_listing_page(self, *, filters: dict | None = None, page: int = 1,
                             page_size: int = 100) -> dict:
        """One server-side page of the Clinic-unit Email listing: filtered COUNT plus
        LIMIT/OFFSET rows. Never loads the whole listing into Python."""
        page_size = max(1, min(int(page_size), 500))
        empty = {"rows": [], "total": 0, "page": 1, "page_size": page_size, "pages": 1}
        if not self.available():
            return empty
        where, args = self._primary_listing_where(filters)
        base = (
            " FROM email_research.clinic_email_status s"
            " LEFT JOIN email_research.email_candidates p"
            "   ON p.clinic_id=s.clinic_id AND p.candidate_id=s.primary_email_candidate_id"
            " LEFT JOIN email_research.clinic_targets t ON t.clinic_id=s.clinic_id"
            + where
        )
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT count(*)" + base, args)
                total = int(cur.fetchone()[0] or 0)
                pages = max(1, -(-total // page_size))
                page = max(1, min(int(page), pages))
                cur.execute(
                    "SELECT s.clinic_id, t.clinic_name, t.prefecture, t.city, s.research_status,"
                    " p.email, p.role, p.ownership, p.email_ownership, p.discovery_source,"
                    " p.source_owner_type, p.source_url, p.confidence, p.auto_decision,"
                    " p.hard_block_codes, p.final_sales_eligible, s.updated_at"
                    + base + " ORDER BY s.updated_at DESC, s.clinic_id DESC LIMIT %s OFFSET %s",
                    [*args, page_size, (page - 1) * page_size],
                )
                rows = cur.fetchall()
                columns = [item.name for item in cur.description]
            self._conn.commit()
            return {"rows": [dict(zip(columns, row)) for row in rows], "total": total,
                    "page": page, "page_size": page_size, "pages": pages}
        except Exception:
            self._safe_rollback()
            raise

    def listing_prefectures(self) -> list[str]:
        """Distinct prefectures of Email-tracked Clinics, for the listing filter."""
        if not self.available():
            return []
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT t.prefecture FROM email_research.clinic_targets t"
                    " JOIN email_research.clinic_email_status s ON s.clinic_id=t.clinic_id"
                    " WHERE COALESCE(t.prefecture,'')<>'' ORDER BY t.prefecture"
                )
                values = [row[0] for row in cur.fetchall()]
            self._conn.commit()
            return values
        except Exception:
            self._safe_rollback()
            raise

    def set_primary_candidate(self, *, clinic_id: int, candidate_id: int | None) -> None:
        self._check_cohort(clinic_id)
        try:
            with self._conn.cursor() as cur:
                if candidate_id is not None:
                    cur.execute(
                        """
                        SELECT clinic_id,final_sales_eligible FROM email_research.email_candidates
                        WHERE candidate_id=%s FOR UPDATE
                        """, (int(candidate_id),),
                    )
                    row = cur.fetchone()
                    if not row or int(row[0]) != int(clinic_id):
                        raise ValueError("別ClinicのEmail CandidateはPrimaryにできません。")
                    if row[1] is not True:
                        raise ValueError("Sales eligibilityを満たさないEmail CandidateはPrimaryにできません。")
                cur.execute(
                    """
                    UPDATE email_research.clinic_email_status
                    SET primary_email_candidate_id=%s,updated_at=now() WHERE clinic_id=%s
                    """, (candidate_id, int(clinic_id)),
                )
                if cur.rowcount != 1:
                    raise ValueError("Clinic Email Statusが見つかりません。")
            self._conn.commit()
        except Exception:
            self._safe_rollback()
            raise

    @_retry_closed_read_connection
    def summary(self) -> dict:
        if not self.available():
            return {"available": False, "total": 0, "processed": 0, "remaining": 0}
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT count(*),
                      count(*) FILTER(WHERE research_status NOT IN ('NOT_RESEARCHED','RUNNING')),
                      count(*) FILTER(WHERE research_status='NOT_RESEARCHED'),
                      count(*) FILTER(WHERE research_status='RUNNING'),
                      count(*) FILTER(WHERE research_status='NO_EMAIL_FOUND'),
                      count(*) FILTER(WHERE research_status='REVIEW'),
                      count(*) FILTER(WHERE research_status='ERROR'),
                      count(*) FILTER(WHERE primary_email_candidate_id IS NOT NULL),
                      count(*) FILTER(WHERE research_status='FOUND')
                    FROM email_research.clinic_email_status
                    """
                )
                values = cur.fetchone()
                cur.execute(
                    """
                    SELECT count(*)
                    FROM email_research.clinic_targets c
                    LEFT JOIN email_research.clinic_email_status s ON s.clinic_id=c.clinic_id
                    WHERE c.eligible=true AND c.manual_exclude=false
                      AND COALESCE(BTRIM(c.official_hp_url),'')<>''
                      AND (s.clinic_id IS NULL OR s.research_status='NOT_RESEARCHED')
                      AND s.primary_email_candidate_id IS NULL
                    """
                )
                newly_eligible = int(cur.fetchone()[0] or 0)
                cur.execute(
                    """
                    SELECT count(*)
                    FROM email_research.clinic_email_status s
                    JOIN email_research.email_candidates p
                      ON p.clinic_id=s.clinic_id
                     AND p.candidate_id=s.primary_email_candidate_id
                    WHERE p.final_sales_eligible=true
                    """
                )
                sales_primary = int(cur.fetchone()[0] or 0)
                cur.execute(
                    """
                    SELECT count(DISTINCT clinic_id),count(*),
                      count(*) FILTER(WHERE auto_decision='AUTO_TRUE'),
                      count(*) FILTER(WHERE auto_decision='REVIEW')
                    FROM email_research.email_candidates
                    """
                )
                candidates = cur.fetchone()
                cur.execute(
                    """
                    SELECT COALESCE(sum((metrics->>'search_query_count')::numeric),0),
                      COALESCE(sum((metrics->>'http_attempts')::numeric),0),
                      COALESCE(sum((metrics->>'web_fetch_attempts')::numeric),0),
                      COALESCE(sum((metrics->>'retry_count')::numeric),0),
                      COALESCE(sum((metrics->>'timeout_count')::numeric),0),
                      COALESCE(sum((metrics->>'elapsed_seconds')::numeric),0),
                      COALESCE(sum((metrics->>'candidate_found_count')::numeric),0),
                      COALESCE(sum((metrics->>'valid_candidate_count')::numeric),0),
                      COALESCE(sum((metrics->>'auto_false_count')::numeric),0)
                    FROM email_research.clinic_email_status
                    WHERE research_status NOT IN ('NOT_RESEARCHED','RUNNING')
                    """
                )
                metric_values = cur.fetchone()
                cur.execute(
                    """
                    SELECT
                      count(*) FILTER(WHERE COALESCE((metrics->>'search_query_count')::int,0)>=1),
                      count(*) FILTER(WHERE COALESCE((metrics->>'search_query_count')::int,0)>=2),
                      count(*) FILTER(WHERE COALESCE((metrics->>'search_query_count')::int,0)=0),
                      COALESCE(sum((metrics->>'query_1_candidate_count')::numeric),0),
                      COALESCE(sum((metrics->>'query_2_candidate_count')::numeric),0)
                    FROM email_research.clinic_email_status
                    WHERE research_status NOT IN ('NOT_RESEARCHED','RUNNING')
                    """
                )
                query_values = cur.fetchone()
            self._conn.commit()
            found = int(values[8] or 0)
            processed_count = int(values[1] or 0)
            running_count = int(values[3] or 0)
            reviewed_count = int(values[5] or 0)
            no_email_count = int(values[4] or 0)
            return {
                "available": True,
                "total": processed_count + running_count + newly_eligible,
                "processed": processed_count,
                "remaining": newly_eligible,
                "not_researched": newly_eligible,
                "running": running_count, "no_email_found": no_email_count,
                "review": reviewed_count, "error": values[6], "found": found,
                "email_researched": found + reviewed_count + no_email_count,
                "primary": sales_primary, "email_obtained": sales_primary,
                "candidate_clinics": candidates[0], "candidate_count": candidates[1],
                "true": candidates[2], "candidate_review": candidates[3],
                "false": metric_values[8], "candidate_found_count": metric_values[6],
                "valid_candidate_count": metric_values[7],
                "search_query_count": metric_values[0], "http_attempts": metric_values[1],
                "web_fetch_attempts": metric_values[2], "retry_count": metric_values[3],
                "timeout_count": metric_values[4], "elapsed_seconds": metric_values[5],
                "query1_called": query_values[0], "query2_called": query_values[1],
                "search_0_count": query_values[2], "query1_candidates": query_values[3],
                "query2_candidates": query_values[4],
            }
        except Exception:
            self._safe_rollback()
            raise

    @_retry_closed_read_connection
    def clinic_status(self, clinic_id: int) -> dict | None:
        """Read the independent Email status and selected Primary for a Clinic detail view."""
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT s.research_status,s.researched_at,s.last_error,
                      p.candidate_id,p.email,p.auto_decision,p.confidence,p.role,p.ownership,
                      p.discovery_source,p.source_url,
                      max(latest_candidate.auto_decision) AS latest_auto_decision,
                      max(latest_candidate.confidence) AS latest_confidence,
                      max(latest_candidate.discovery_source) AS latest_source,
                      max(latest_candidate.source_url) AS latest_source_url,
                      count(e.candidate_id) FILTER(WHERE e.auto_decision='REVIEW'
                        AND latest.id IS NULL) AS unreviewed
                    FROM email_research.clinic_email_status s
                    LEFT JOIN email_research.email_candidates p
                      ON p.clinic_id=s.clinic_id AND p.candidate_id=s.primary_email_candidate_id
                    LEFT JOIN email_research.email_candidates e ON e.clinic_id=s.clinic_id
                    LEFT JOIN LATERAL (
                      SELECT candidate_id,auto_decision,confidence,discovery_source,source_url
                      FROM email_research.email_candidates
                      WHERE clinic_id=s.clinic_id
                      ORDER BY researched_at DESC,candidate_id DESC LIMIT 1
                    ) latest_candidate ON true
                    LEFT JOIN LATERAL (
                      SELECT h.id FROM provenance.email_human_reviews h
                      WHERE h.candidate_id=e.candidate_id
                      ORDER BY h.reviewed_at DESC,h.id DESC LIMIT 1
                    ) latest ON true
                    WHERE s.clinic_id=%s
                    GROUP BY s.clinic_id,p.candidate_id
                    """, (int(clinic_id),),
                )
                row = cur.fetchone()
                result = dict(zip([item.name for item in cur.description], row)) if row else None
            self._conn.commit()
            return result
        except Exception:
            self._safe_rollback()
            raise

    @_retry_closed_read_connection
    def researched_clinic_ids(self) -> set[int]:
        """Historical/active work exclusions, sourced only from the Email sidecar."""
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT clinic_id FROM email_research.clinic_email_status
                    WHERE research_status<>'NOT_RESEARCHED' OR candidate_count>0
                       OR primary_email_candidate_id IS NOT NULL
                    UNION
                    SELECT clinic_id FROM email_research.email_candidates
                    UNION
                    SELECT clinic_id FROM email_research.clinic_targets
                    WHERE manual_exclude=true
                    """
                )
                values = {int(row[0]) for row in cur.fetchall()}
            self._conn.commit()
            return values
        except Exception:
            self._safe_rollback()
            raise

    @_retry_closed_read_connection
    def latest_auto_run(self) -> dict | None:
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT * FROM email_research.email_auto_runs ORDER BY started_at DESC LIMIT 1")
                row = cur.fetchone()
                result = dict(zip([item.name for item in cur.description], row)) if row else None
            self._conn.commit()
            return result
        except Exception:
            self._safe_rollback()
            raise

    def create_auto_run(self, *, workers=12, timeout=20, retries=1, search_limit=2) -> str:
        run_id = uuid.uuid4().hex
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO email_research.email_auto_runs(
                      run_id,status,worker_count,search_query_limit,read_timeout_seconds,
                      retry_count,rule_version
                    ) VALUES(%s,'RUNNING',%s,%s,%s,%s,%s)
                    """, (run_id, int(workers), min(2, int(search_limit)), float(timeout),
                            int(retries), RULE_VERSION),
                )
            self._conn.commit()
            return run_id
        except Exception:
            self._safe_rollback()
            raise

    def acquire_controller(self, run_id: str, controller_id: str,
                           lease_seconds: int = EMAIL_CONTROLLER_LEASE_SECONDS) -> dict | None:
        """Atomically acquire an empty/expired persisted controller lease for one run."""
        controller_id = str(controller_id or "").strip()
        if not controller_id:
            raise ValueError("Email controller_idを指定してください。")
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT status,stop_requested,metrics,clock_timestamp() "
                    "FROM email_research.email_auto_runs WHERE run_id=%s FOR UPDATE",
                    (str(run_id),),
                )
                row = cur.fetchone()
                if not row or row[0] != "RUNNING" or bool(row[1]):
                    self._conn.commit()
                    return None
                metrics = _json_dict(row[2])
                db_now = row[3] if row[3].tzinfo else row[3].replace(tzinfo=timezone.utc)
                owner = str(metrics.get("controller_id") or "")
                lease = _parse_timestamp(metrics.get("controller_lease_until"))
                if owner and owner != controller_id and lease > db_now:
                    self._conn.commit()
                    return None
                if owner != controller_id or lease <= db_now:
                    metrics["controller_epoch"] = int(metrics.get("controller_epoch", 0) or 0) + 1
                metrics.update({
                    "controller_id": controller_id,
                    "controller_heartbeat_at": db_now.isoformat(timespec="seconds"),
                    "controller_lease_until": (db_now + timedelta(
                        seconds=max(30, int(lease_seconds))
                    )).isoformat(timespec="seconds"),
                })
                cur.execute(
                    "UPDATE email_research.email_auto_runs SET metrics=%s,updated_at=now() WHERE run_id=%s",
                    (Jsonb(metrics), str(run_id)),
                )
            self._conn.commit()
            return {"run_id": str(run_id), "status": "RUNNING", "metrics": metrics}
        except Exception:
            self._safe_rollback()
            raise

    def heartbeat_controller(self, run_id: str, controller_id: str,
                             lease_seconds: int = EMAIL_CONTROLLER_LEASE_SECONDS) -> bool:
        """Renew only a live lease; an expired process must reacquire and be fenced."""
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT status,stop_requested,metrics,clock_timestamp() "
                    "FROM email_research.email_auto_runs WHERE run_id=%s FOR UPDATE",
                    (str(run_id),),
                )
                row = cur.fetchone()
                if not row or row[0] != "RUNNING" or bool(row[1]):
                    self._conn.commit()
                    return False
                metrics = _json_dict(row[2])
                db_now = row[3] if row[3].tzinfo else row[3].replace(tzinfo=timezone.utc)
                if (str(metrics.get("controller_id") or "") != str(controller_id)
                        or _parse_timestamp(metrics.get("controller_lease_until")) <= db_now):
                    self._conn.commit()
                    return False
                metrics.update({
                    "controller_heartbeat_at": db_now.isoformat(timespec="seconds"),
                    "controller_lease_until": (db_now + timedelta(
                        seconds=max(30, int(lease_seconds))
                    )).isoformat(timespec="seconds"),
                })
                cur.execute(
                    "UPDATE email_research.email_auto_runs SET metrics=%s,updated_at=now() WHERE run_id=%s",
                    (Jsonb(metrics), str(run_id)),
                )
            self._conn.commit()
            return True
        except Exception:
            self._safe_rollback()
            raise

    def finish_controller(self, run_id: str, controller_id: str, *, status: str,
                          error: str = "") -> bool:
        """Fenced final transition; a superseded process cannot finish another owner."""
        if status not in {"COMPLETED", "ERROR"}:
            raise ValueError(status)
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE email_research.email_auto_runs
                    SET status=%s,finished_at=now(),updated_at=now(),last_error=%s,
                        metrics=(COALESCE(metrics,'{}'::jsonb)
                          - 'controller_id' - 'controller_lease_until')
                    WHERE run_id=%s AND status='RUNNING'
                      AND metrics->>'controller_id'=%s
                    """,
                    (status, str(error or "")[:4000], str(run_id), str(controller_id)),
                )
                changed = cur.rowcount == 1
            self._conn.commit()
            return changed
        except Exception:
            self._safe_rollback()
            raise

    @_retry_closed_read_connection
    def auto_run(self, run_id: str) -> dict | None:
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT * FROM email_research.email_auto_runs WHERE run_id=%s", (str(run_id),))
                row = cur.fetchone()
                result = dict(zip([c.name for c in cur.description], row)) if row else None
            self._conn.commit()
            return result
        except Exception:
            self._safe_rollback()
            raise

    def update_auto_run(self, run_id: str, *, status=None, metrics=None, error="", stop_requested=None):
        fields = ["updated_at=now()"]
        params = []
        if status is not None:
            fields.append("status=%s"); params.append(str(status))
            if status in {"COMPLETED", "ERROR"}:
                fields.append("finished_at=now()")
            if status in {"PAUSED", "STOPPED", "COMPLETED", "ERROR"} and metrics is None:
                fields.append("metrics=(COALESCE(metrics,'{}'::jsonb)-'controller_id'-'controller_lease_until')")
        if metrics is not None:
            fields.append("metrics=%s"); params.append(Jsonb(metrics))
        if error:
            fields.append("last_error=%s"); params.append(str(error)[:4000])
        if stop_requested is not None:
            fields.append("stop_requested=%s"); params.append(bool(stop_requested))
        params.append(str(run_id))
        try:
            with self._conn.cursor() as cur:
                cur.execute("UPDATE email_research.email_auto_runs SET " + ",".join(fields) + " WHERE run_id=%s", params)
            self._conn.commit()
        except Exception:
            self._safe_rollback()
            raise

    def record_auto_run_result(self, run_id: str, *, metrics=None, research_status="ERROR"):
        values = metrics or {}
        search_count = int(values.get("search_query_count", 0) or 0)
        terminal = str(research_status or "ERROR").upper()
        increments = {
            "processed": 1,
            "found": int(terminal == "FOUND"),
            "review": int(terminal == "REVIEW"),
            "no_email": int(terminal == "NO_EMAIL_FOUND"),
            "error": int(terminal == "ERROR"),
            "query1_called": int(search_count >= 1),
            "query2_called": int(search_count >= 2),
            "search_0_count": int(search_count == 0),
            "search_query_count": search_count,
            "http_attempts": int(values.get("http_attempts", 0) or 0),
            "web_fetch_attempts": int(values.get("web_fetch_attempts", 0) or 0),
            "timeout_count": int(values.get("timeout_count", 0) or 0),
            "retry_count": int(values.get("retry_count", 0) or 0),
            "elapsed_seconds": float(values.get("elapsed_seconds", 0) or 0),
            "candidate_found_count": int(values.get("candidate_found_count", 0) or 0),
            "valid_candidate_count": int(values.get("valid_candidate_count", 0) or 0),
            "auto_true_count": int(values.get("auto_true_count", 0) or 0),
            "auto_false_count": int(values.get("auto_false_count", 0) or 0),
            "review_candidate_count": int(values.get("review_count", 0) or 0),
            "claim_ms": float(values.get("claim_ms", 0) or 0),
            "db_read_ms": float(values.get("db_read_ms", 0) or 0),
            "db_write_ms": float(values.get("db_write_ms", 0) or 0),
            "target_sync_ms": float(values.get("target_sync_ms", 0) or 0),
            "elapsed_ms": float(values.get("elapsed_ms", 0) or 0),
        }
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE email_research.email_auto_runs r
                    SET metrics=COALESCE(r.metrics,'{}'::jsonb) || jsonb_build_object(
                      'processed',COALESCE((r.metrics->>'processed')::int,0)+%s,
                      'found',COALESCE((r.metrics->>'found')::int,0)+%s,
                      'review',COALESCE((r.metrics->>'review')::int,0)+%s,
                      'no_email',COALESCE((r.metrics->>'no_email')::int,0)+%s,
                      'error',COALESCE((r.metrics->>'error')::int,0)+%s,
                      'query1_called',COALESCE((r.metrics->>'query1_called')::int,0)+%s,
                      'query2_called',COALESCE((r.metrics->>'query2_called')::int,0)+%s,
                      'search_0_count',COALESCE((r.metrics->>'search_0_count')::int,0)+%s,
                      'search_query_count',COALESCE((r.metrics->>'search_query_count')::int,0)+%s,
                      'http_attempts',COALESCE((r.metrics->>'http_attempts')::int,0)+%s,
                      'web_fetch_attempts',COALESCE((r.metrics->>'web_fetch_attempts')::int,0)+%s,
                      'timeout_count',COALESCE((r.metrics->>'timeout_count')::int,0)+%s,
                      'retry_count',COALESCE((r.metrics->>'retry_count')::int,0)+%s,
                      'elapsed_seconds',COALESCE((r.metrics->>'elapsed_seconds')::numeric,0)+%s,
                      'candidate_found_count',COALESCE((r.metrics->>'candidate_found_count')::int,0)+%s,
                      'valid_candidate_count',COALESCE((r.metrics->>'valid_candidate_count')::int,0)+%s,
                      'auto_true_count',COALESCE((r.metrics->>'auto_true_count')::int,0)+%s,
                      'auto_false_count',COALESCE((r.metrics->>'auto_false_count')::int,0)+%s,
                      'review_candidate_count',COALESCE((r.metrics->>'review_candidate_count')::int,0)+%s,
                      'claim_ms',COALESCE((r.metrics->>'claim_ms')::numeric,0)+%s,
                      'db_read_ms',COALESCE((r.metrics->>'db_read_ms')::numeric,0)+%s,
                      'db_write_ms',COALESCE((r.metrics->>'db_write_ms')::numeric,0)+%s,
                      'target_sync_ms',COALESCE((r.metrics->>'target_sync_ms')::numeric,0)+%s,
                      'elapsed_ms',COALESCE((r.metrics->>'elapsed_ms')::numeric,0)+%s
                    ),updated_at=now()
                    WHERE r.run_id=%s
                    """,
                    tuple(increments.values()) + (str(run_id),),
                )
            self._conn.commit()
        except Exception:
            self._safe_rollback()
            raise

    def record_target_sync(self, run_id: str, *, elapsed_ms: float):
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE email_research.email_auto_runs
                    SET metrics=COALESCE(metrics,'{}'::jsonb) || jsonb_build_object(
                      'target_sync_ms',COALESCE((metrics->>'target_sync_ms')::numeric,0)+%s
                    ),updated_at=now()
                    WHERE run_id=%s
                    """, (max(0.0, float(elapsed_ms)), str(run_id)),
                )
            self._conn.commit()
        except Exception:
            self._safe_rollback()
            raise
