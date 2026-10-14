"""Append-only Email Human Review labels and training rows."""
from __future__ import annotations

import json
import threading

from psycopg.types.json import Jsonb

from src.master.email_sales_eligibility import evaluate_sales_eligibility
from src.repository.email_research_repository import _retry_closed_read_connection


TABLE_NAME = "provenance.email_human_reviews"
VALID_HUMAN_DECISIONS = frozenset({"TRUE_POSITIVE", "FALSE_POSITIVE", "UNCERTAIN"})


def _as_dict(value):
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    return json.loads(str(value))


class SupabaseEmailHumanReviewRepository:
    def __init__(self, conn, *, connect_fn=None):
        self._conn = conn
        # This repository is handed the same physical connection as
        # SupabaseEmailResearchRepository (see email_database._bundle_for_connection),
        # but each reconnects independently -- a rebuild here does not need to coordinate
        # with the sibling repository, it only needs its own self._conn to stay usable.
        self._connect_fn = connect_fn
        self._reconnect_lock = threading.Lock()

    def _connection_is_dead(self) -> bool:
        return bool(getattr(self._conn, "closed", False) or getattr(self._conn, "broken", False))

    def _is_retryable_connection_error(self, exc) -> bool:
        try:
            import psycopg
        except ImportError:
            return False
        return isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError))

    def _safe_rollback(self) -> None:
        try:
            if not getattr(self._conn, "closed", False):
                self._conn.rollback()
        except Exception:
            pass

    def _reconnect_read(self) -> None:
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
        """``available()`` is a bool API, so a reconnect attempt that itself fails to
        reach the DB (connection was healthy when this repository was cached, then the
        DB/network dropped, and the follow-up reconnect also fails) must resolve to
        ``False`` here -- not propagate -- the same as the "could reach the DB but the
        schema isn't there yet" case below already does. Only connectivity-shaped
        exceptions are caught; a bug in `_ensure_read_connection()` itself (TypeError,
        AttributeError, ...) still raises so it is not hidden.
        """
        try:
            self._ensure_read_connection()
        except Exception as exc:
            if not self._is_retryable_connection_error(exc):
                raise
            return False
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT to_regclass(%s)", (TABLE_NAME,))
                exists = cur.fetchone()[0] is not None
            self._conn.commit()
            return exists
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
                    cur.execute("SELECT to_regclass(%s)", (TABLE_NAME,))
                    exists = cur.fetchone()[0] is not None
                self._conn.commit()
                return exists
            except Exception:
                self._safe_rollback()
                return False

    @_retry_closed_read_connection
    def queue(self, *, include_reviewed=False, limit=200, clinic_id=None):
        if not self.available():
            return []
        reviewed_filter = "" if include_reviewed else "AND latest.id IS NULL"
        clinic_filter = "" if clinic_id is None else "AND e.clinic_id=%s"
        limit_sql = "" if limit is None else " LIMIT %s"
        params = (() if clinic_id is None else (int(clinic_id),)) + (() if limit is None else (max(0, int(limit)),))
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT c.clinic_id,c.clinic_uuid,c.clinic_name,c.prefecture,c.city,c.address,c.phone,
                      c.official_hp_url AS hp_url,
                      e.candidate_id,e.email,e.discovery_source,e.source_url,e.search_query,
                      e.auto_decision,e.auto_identity_decision,e.confidence,e.role,e.ownership,
                      e.email_ownership,e.source_owner_type,
                      e.verification_status,e.clinic_name_match,e.prefecture_match,e.city_match,
                      e.verification_reason,e.rejection_reason,e.hard_block_codes,
                      e.final_sales_eligible,e.auto_snapshot,e.rule_version,e.researched_at,
                      s.primary_email_candidate_id,
                      latest.id AS human_review_id,latest.human_decision,latest.reviewer,
                      latest.review_note,latest.reviewed_at
                    FROM email_research.email_candidates e
                    JOIN email_research.clinic_targets c ON c.clinic_id=e.clinic_id
                    JOIN email_research.clinic_email_status s ON s.clinic_id=e.clinic_id
                    LEFT JOIN LATERAL (
                      SELECT h.id,h.human_decision,h.reviewer,h.review_note,h.reviewed_at
                      FROM provenance.email_human_reviews h
                      WHERE h.candidate_id=e.candidate_id
                      ORDER BY h.reviewed_at DESC,h.id DESC LIMIT 1
                    ) latest ON true
                    WHERE e.auto_decision='REVIEW'
                    """ + reviewed_filter + " " + clinic_filter + " ORDER BY "
                    "CASE e.confidence WHEN 'HIGH' THEN 0 WHEN 'MEDIUM' THEN 1 ELSE 2 END,"
                    "e.researched_at,e.candidate_id" + limit_sql,
                    params,
                )
                columns = [item.name for item in cur.description]
                rows = [dict(zip(columns, row)) for row in cur.fetchall()]
            self._conn.commit()
            for row in rows:
                row["auto_snapshot"] = _as_dict(row.get("auto_snapshot"))
                row["reviewed"] = bool(row.get("human_review_id"))
            return rows
        except Exception:
            self._safe_rollback()
            raise

    @_retry_closed_read_connection
    def summary(self):
        if not self.available():
            return {"available": False, "unreviewed": 0, "reviewed": 0}
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                      count(*) FILTER(WHERE latest.id IS NULL),
                      count(*) FILTER(WHERE latest.id IS NOT NULL),
                      count(DISTINCT e.clinic_id) FILTER(WHERE latest.id IS NULL)
                    FROM email_research.email_candidates e
                    LEFT JOIN LATERAL (
                      SELECT h.id FROM provenance.email_human_reviews h
                      WHERE h.candidate_id=e.candidate_id
                      ORDER BY h.reviewed_at DESC,h.id DESC LIMIT 1
                    ) latest ON true
                    WHERE e.auto_decision='REVIEW'
                    """
                )
                row = cur.fetchone()
            self._conn.commit()
            return {
                "available": True, "unreviewed": row[0], "reviewed": row[1],
                "unreviewed_clinics": row[2],
            }
        except Exception:
            self._safe_rollback()
            raise

    def save_review(self, *, candidate_id, human_decision, reviewer, review_note="") -> int:
        if human_decision not in VALID_HUMAN_DECISIONS:
            raise ValueError("Email Human Reviewの判定値を確認してください。")
        reviewer = str(reviewer or "").strip()
        if not reviewer:
            raise ValueError("確認者を入力してください。")
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT e.*,s.primary_email_candidate_id
                    FROM email_research.email_candidates e
                    JOIN email_research.clinic_email_status s ON s.clinic_id=e.clinic_id
                    WHERE e.candidate_id=%s FOR UPDATE OF e,s
                    """, (int(candidate_id),),
                )
                row = cur.fetchone()
                if not row:
                    raise ValueError("Email Candidateが見つかりません。")
                columns = [item.name for item in cur.description]
                candidate = dict(zip(columns, row))
                snapshot = _as_dict(candidate.get("auto_snapshot"))
                snapshot.update({
                    "candidate_id": candidate["candidate_id"],
                    "email": candidate["email"],
                    "hard_block_codes": list(candidate.get("hard_block_codes") or []),
                })
                result = evaluate_sales_eligibility(
                    auto_identity_decision=candidate["auto_identity_decision"],
                    human_decision=human_decision,
                    role=candidate["role"], ownership=candidate.get("email_ownership") or candidate["ownership"],
                    clinic_name_match=candidate["clinic_name_match"],
                    prefecture_match=candidate["prefecture_match"],
                    city_match=candidate["city_match"],
                    discovery_source=candidate["discovery_source"],
                    confidence=candidate["confidence"],
                    extra_hard_block_codes=candidate.get("hard_block_codes") or (),
                )
                cur.execute(
                    """
                    INSERT INTO provenance.email_human_reviews(
                      clinic_id,candidate_id,email,human_decision,reviewer,review_note,
                      auto_role,auto_ownership,auto_verification_status,
                      auto_identity_decision,auto_confidence,auto_final_sales_eligible,
                      auto_primary_email_candidate_id,auto_snapshot,rule_version,reviewed_at
                    ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                    RETURNING id
                    """,
                    (candidate["clinic_id"], candidate["candidate_id"], candidate["email"],
                     human_decision, reviewer, str(review_note or "").strip(),
                     candidate["role"], candidate.get("email_ownership") or candidate["ownership"], candidate["verification_status"],
                     candidate["auto_identity_decision"], candidate["confidence"],
                     candidate["final_sales_eligible"], candidate.get("primary_email_candidate_id"),
                     Jsonb(snapshot), candidate["rule_version"]),
                )
                review_id = int(cur.fetchone()[0])
                cur.execute(
                    """
                    UPDATE email_research.email_candidates
                    SET final_sales_eligible=%s,sales_usable=%s,updated_at=now()
                    WHERE candidate_id=%s
                    """, (result.final_sales_eligible, result.sales_usable, int(candidate_id)),
                )
                if not result.final_sales_eligible:
                    cur.execute(
                        """
                        UPDATE email_research.clinic_email_status
                        SET primary_email_candidate_id=NULL,updated_at=now()
                        WHERE clinic_id=%s AND primary_email_candidate_id=%s
                        """, (candidate["clinic_id"], int(candidate_id)),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE email_research.clinic_email_status
                        SET primary_email_candidate_id=%s,updated_at=now()
                        WHERE clinic_id=%s AND primary_email_candidate_id IS NULL
                        """, (int(candidate_id), candidate["clinic_id"]),
                    )
            self._conn.commit()
            return review_id
        except Exception:
            self._safe_rollback()
            raise

    @_retry_closed_read_connection
    def training_rows(self):
        if not self.available():
            return []
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT ON(h.candidate_id)
                      h.id,h.clinic_id,c.clinic_name,c.prefecture,c.city,c.address,c.phone,
                      h.candidate_id,h.email,h.human_decision,h.reviewer,h.review_note,
                      h.auto_role,h.auto_ownership,h.auto_verification_status,
                      h.auto_identity_decision,h.auto_confidence,h.auto_final_sales_eligible,
                      h.auto_primary_email_candidate_id,h.auto_snapshot,h.rule_version,h.reviewed_at
                    FROM provenance.email_human_reviews h
                    JOIN email_research.clinic_targets c ON c.clinic_id=h.clinic_id
                    ORDER BY h.candidate_id,h.reviewed_at DESC,h.id DESC
                    """
                )
                columns = [item.name for item in cur.description]
                rows = [dict(zip(columns, row)) for row in cur.fetchall()]
            self._conn.commit()
            return rows
        except Exception:
            self._safe_rollback()
            raise
