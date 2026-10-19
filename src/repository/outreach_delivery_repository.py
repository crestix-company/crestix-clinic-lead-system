"""Narrow Postgres boundary for the email delivery ledger.

This repository uses the existing Clinic runtime login only to assume the dedicated
``clinic_outreach_runtime`` role. The assumed role has table-scoped SELECT/INSERT/UPDATE,
no DELETE, and is checked on every connection. This module never falls back to SQLite.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import os
import uuid
from typing import Any, Callable

from src.repository.connection_diagnostics import connection_error_category, retryable_read_connection_error

TABLE = "outreach.email_delivery_records"
RUNTIME_ROLE = "clinic_runtime"
OUTREACH_ROLE = "clinic_outreach_runtime"
BLOCKING_STATUSES = frozenset({"DRAFT", "SENT", "SCHEDULED", "REPLIED", "TRASHED", "UNKNOWN", "REVIEW", "FAILED"})


class OutreachDatabaseError(RuntimeError):
    """Sanitized fail-closed connection or permission error."""


@dataclass(frozen=True)
class DraftReservation:
    outreach_id: str
    operation_id: str
    idempotency_key: str
    created: bool
    status: str
    gmail_draft_id: str = ""


def _idempotency_key(clinic_id: int, campaign_id: str) -> str:
    material = f"clinic-outreach-v1|{int(clinic_id)}|{campaign_id.strip()}".encode()
    return hashlib.sha256(material).hexdigest()


def connect_outreach_database(*, environ=None, connect_fn: Callable[..., Any] | None = None):
    """Connect through the existing runtime URL, then assume the restricted table role."""
    env = os.environ if environ is None else environ
    url = (env.get("SUPABASE_RUNTIME_DB_URL") or "").strip()
    if not url:
        raise OutreachDatabaseError("Supabase runtime connection is not configured")
    if connect_fn is None:
        try:
            import psycopg
            conn = psycopg.connect(url, connect_timeout=5)
        except Exception as exc:
            raise OutreachDatabaseError(
                f"Supabase connection failed ({connection_error_category(exc)})"
            ) from None
    else:
        conn = connect_fn(url, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_user")
            original_user = str(cur.fetchone()[0] or "").casefold()
            if original_user != RUNTIME_ROLE:
                raise OutreachDatabaseError("Outreach ledger requires the clinic_runtime database login")
            cur.execute("SET ROLE clinic_outreach_runtime")
            cur.execute(
                """SELECT current_user, current_role,
                          has_table_privilege(current_user, %s, 'SELECT'),
                          has_any_column_privilege(current_user, %s, 'INSERT'),
                          has_any_column_privilege(current_user, %s, 'UPDATE'),
                          has_table_privilege(current_user, %s, 'DELETE'),
                          has_table_privilege(current_user, %s, 'TRUNCATE'),
                          (SELECT rolbypassrls FROM pg_roles WHERE rolname=current_role),
                          (SELECT rolsuper FROM pg_roles WHERE rolname=current_role),
                          (SELECT rolcreatedb FROM pg_roles WHERE rolname=current_role),
                          (SELECT rolcreaterole FROM pg_roles WHERE rolname=current_role)""",
                (TABLE, TABLE, TABLE, TABLE, TABLE),
            )
            row = cur.fetchone()
            if not row or str(row[0]).casefold() != OUTREACH_ROLE or str(row[1]).casefold() != OUTREACH_ROLE:
                raise OutreachDatabaseError("Restricted outreach role assumption failed")
            if (not all(bool(row[i]) for i in (2, 3, 4))
                    or any(bool(row[i]) for i in range(5, 11))):
                raise OutreachDatabaseError("Outreach role privileges are not restricted as required")
            cur.execute("SELECT 1 FROM outreach.email_delivery_records LIMIT 0")
        conn.commit()
        return conn
    except OutreachDatabaseError:
        conn.rollback()
        conn.close()
        raise
    except Exception as exc:
        conn.rollback()
        conn.close()
        raise OutreachDatabaseError(
            f"Outreach ledger preflight failed ({connection_error_category(exc)})"
        ) from None


class OutreachDeliveryRepository:
    """Bounded history reads and explicit no-retry writes on a restricted connection."""

    def __init__(self, conn, reconnect_factory=None):
        self._conn = conn
        self._reconnect_factory = reconnect_factory

    @classmethod
    def connect(cls, *, environ=None, connect_fn=None):
        factory = lambda: connect_outreach_database(environ=environ, connect_fn=connect_fn)
        return cls(factory(), reconnect_factory=factory)

    def close(self):
        self._conn.close()

    def health_check(self) -> bool:
        rows = self._select("SELECT 1")
        return bool(rows and rows[0][0] == 1)

    def _select(self, sql: str, params: tuple = (), *, row_factory=None):
        """Replay a read once after a transport failure; write methods never call this."""
        for attempt in range(2):
            try:
                kwargs = {"row_factory": row_factory} if row_factory is not None else {}
                with self._conn.cursor(**kwargs) as cur:
                    cur.execute(sql, params)
                    rows = cur.fetchall()
                self._conn.commit()
                return rows
            except Exception as exc:
                self._conn.rollback()
                if attempt == 0 and self._reconnect_factory and retryable_read_connection_error(exc):
                    try:
                        self._conn.close()
                    except Exception:
                        pass
                    try:
                        self._conn = self._reconnect_factory()
                        continue
                    except Exception as reconnect_error:
                        raise OutreachDatabaseError(
                            f"Outreach history read reconnect failed ({connection_error_category(reconnect_error)})"
                        ) from None
                raise OutreachDatabaseError(
                    f"Outreach history read failed ({connection_error_category(exc)})"
                ) from None
        return []

    def campaign_rows(self, campaign_id: str) -> list[dict]:
        if not campaign_id.strip():
            return []
        from psycopg.rows import dict_row
        rows = self._select(
            f"SELECT outreach_id,operation_id,idempotency_key,clinic_id,clinic_uuid,status,"
            f"gmail_draft_id,gmail_message_id,gmail_thread_id,sent_at,scheduled_at,created_at,updated_at "
            f"FROM {TABLE} WHERE campaign_id=%s ORDER BY clinic_id,created_at",
            (campaign_id.strip(),), row_factory=dict_row,
        )
        return [dict(row) for row in rows]

    def campaign_statuses(self, campaign_id: str) -> dict[int, str]:
        return {int(row["clinic_id"]): str(row["status"])
                for row in self.campaign_rows(campaign_id)}

    def clinic_statuses(self, clinic_ids) -> dict[int, list[dict]]:
        """Every delivery record for the given Clinic IDs, across all campaigns.

        READ ONLY cross-campaign dedup lookup (e.g. for a GAS/outreach payload that must
        not re-target a clinic with an existing DRAFT/SENT/SCHEDULED/REPLIED record under
        any campaign_id, not just one known in advance).
        """
        ids = sorted({int(cid) for cid in clinic_ids})
        if not ids:
            return {}
        from psycopg.rows import dict_row
        rows = self._select(
            f"SELECT clinic_id,campaign_id,status,gmail_draft_id,gmail_message_id,"
            f"sent_at,created_at FROM {TABLE} WHERE clinic_id=ANY(%s::bigint[]) "
            f"ORDER BY clinic_id,created_at",
            (ids,), row_factory=dict_row,
        )
        result: dict[int, list[dict]] = {clinic_id: [] for clinic_id in ids}
        for row in rows:
            result[int(row["clinic_id"])].append(dict(row))
        return result

    def counts(self, *, campaign_id: str | None = None) -> dict[str, int]:
        sql = f"SELECT status,count(*) AS n FROM {TABLE}"
        params: tuple = ()
        if campaign_id:
            sql += " WHERE campaign_id=%s"
            params = (campaign_id.strip(),)
        sql += " GROUP BY status"
        rows = self._select(sql, params)
        return {str(status): int(count) for status, count in rows}

    def summary_counts(self, *, jst_today) -> dict[str, int]:
        from zoneinfo import ZoneInfo
        from datetime import time, timedelta
        jst = ZoneInfo("Asia/Tokyo")
        start = datetime.combine(jst_today, time.min, tzinfo=jst)
        end = start + timedelta(days=1)
        rows = self._select(
                f"""SELECT
                  count(*) FILTER (WHERE status='DRAFT') AS drafts,
                  count(*) FILTER (WHERE status='SENT' AND sent_at >= %s AND sent_at < %s) AS sent_today,
                  count(*) FILTER (WHERE status='SCHEDULED') AS scheduled,
                  count(*) FILTER (WHERE status IN ('REVIEW','UNKNOWN')) AS review,
                  count(*) FILTER (WHERE status='FAILED') AS failed,
                  count(*) AS total
                FROM {TABLE}""",
                (start, end),
            )
        row = rows[0] if rows else (0, 0, 0, 0, 0, 0)
        keys = ("drafts", "sent_today", "scheduled", "review", "failed", "total")
        return {key: int(value or 0) for key, value in zip(keys, row)}

    def reserve_draft(self, *, clinic_id: int, clinic_uuid: str, clinic_name: str,
                      clinic_address: str, recipient_email: str, sender_account: str,
                      staff_name: str, campaign_id: str, generation_batch_id: str,
                      operation_id: str, status: str = "REVIEW", listing_status: str = "UNCONFIRMED",
                      article_type: str = "UNKNOWN", example_article_url: str = "",
                      listing_article_url: str = "", clinic_profile_url: str = "",
                      subject: str = "", body_sha256: str = "") -> DraftReservation:
        if status != "REVIEW":
            raise ValueError("Draft reservation must begin in REVIEW state")
        key = _idempotency_key(clinic_id, campaign_id)
        outreach_id = str(uuid.uuid4())
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                # The permanent per-clinic guard is inserted in the same transaction
                # as the campaign ledger row. Its unique Clinic ID arbitrates races
                # across campaigns; Gmail drafts.create is reachable only by the
                # winner. Existing clinics are preloaded by the guard migration.
                cur.execute(
                    """INSERT INTO outreach.clinic_draft_guard
                    (clinic_id,first_campaign_id,first_outreach_id)
                    VALUES (%s,%s,%s) ON CONFLICT (clinic_id) DO NOTHING
                    RETURNING clinic_id""",
                    (int(clinic_id), campaign_id, outreach_id),
                )
                guard_claimed = cur.fetchone() is not None
                if not guard_claimed:
                    cur.execute(
                        f"SELECT outreach_id,operation_id,status,gmail_draft_id FROM {TABLE} "
                        "WHERE clinic_id=%s AND campaign_id=%s",
                        (int(clinic_id), campaign_id),
                    )
                    existing = cur.fetchone()
                    if existing is None:
                        raise OutreachDatabaseError("Clinic already has a cross-campaign history guard")
                    return DraftReservation(str(existing[0]), str(existing[1]), key, False,
                                            str(existing[2]), str(existing[3] or ""))
                cur.execute(
                    f"""INSERT INTO {TABLE}
                    (outreach_id,operation_id,idempotency_key,clinic_id,clinic_uuid,
                     clinic_name_snapshot,clinic_address_snapshot,recipient_email,sender_account,
                     staff_name,campaign_id,generation_batch_id,status,listing_status,article_type,
                     example_article_url,listing_article_url,clinic_profile_url,subject,body_sha256,last_error)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (clinic_id,campaign_id) DO NOTHING
                    RETURNING outreach_id,operation_id,status,gmail_draft_id""",
                    (outreach_id, operation_id, key, int(clinic_id), clinic_uuid, clinic_name,
                     clinic_address, recipient_email, sender_account, staff_name, campaign_id,
                     generation_batch_id, status, listing_status, article_type, example_article_url,
                     listing_article_url, clinic_profile_url, subject, body_sha256,
                     "Draft operation reserved; Gmail outcome not yet recorded"),
                )
                row = cur.fetchone()
                created = row is not None
                if row is None:
                    cur.execute(
                        f"SELECT outreach_id,operation_id,status,gmail_draft_id FROM {TABLE} "
                        "WHERE clinic_id=%s AND campaign_id=%s",
                        (int(clinic_id), campaign_id),
                    )
                    row = cur.fetchone()
                if row is None:
                    raise OutreachDatabaseError("Outreach reservation result is unavailable")
        return DraftReservation(str(row[0]), str(row[1]), key, created,
                                str(row[2]), str(row[3] or ""))

    def record_draft_result(self, *, outreach_id: str, status: str,
                            gmail_draft_id: str = "", gmail_message_id: str = "",
                            gmail_thread_id: str = "", last_error: str = "") -> bool:
        if status not in {"DRAFT", "FAILED", "UNKNOWN", "REVIEW"}:
            raise ValueError("Invalid draft result status")
        # A write is intentionally attempted once. On connection loss the caller must
        # re-read Gmail and the ledger; it must never repeat drafts.create automatically.
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.execute(
                    f"""UPDATE {TABLE} SET status=%s,gmail_draft_id=NULLIF(%s,''),
                    gmail_message_id=NULLIF(%s,''),gmail_thread_id=NULLIF(%s,''),
                    last_synced_at=now(),last_error=%s
                    WHERE outreach_id=%s AND status IN ('REVIEW','UNKNOWN')""",
                    (status, gmail_draft_id, gmail_message_id, gmail_thread_id,
                     last_error[:500], outreach_id),
                )
                updated = cur.rowcount == 1
        return updated

    def sync_status(self, *, outreach_id: str, status: str, gmail_draft_id: str = "",
                    gmail_message_id: str = "", gmail_thread_id: str = "",
                    sent_at: datetime | None = None, scheduled_at: datetime | None = None,
                    last_error: str = "") -> bool:
        if status not in {"DRAFT", "SENT", "SCHEDULED", "REPLIED", "TRASHED", "FAILED", "UNKNOWN", "REVIEW"}:
            raise ValueError("Invalid synchronized status")
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.execute(
                    f"""UPDATE {TABLE} SET status=%s,gmail_draft_id=NULLIF(%s,''),
                    gmail_message_id=NULLIF(%s,''),gmail_thread_id=NULLIF(%s,''),
                    sent_at=COALESCE(%s,sent_at),scheduled_at=COALESCE(%s,scheduled_at),
                    last_synced_at=now(),last_error=%s WHERE outreach_id=%s""",
                    (status, gmail_draft_id, gmail_message_id, gmail_thread_id,
                     sent_at, scheduled_at, last_error[:500], outreach_id),
                )
                updated = cur.rowcount == 1
        return updated

    def backfill_history_event(self, *, clinic_id: int, clinic_uuid: str | None,
                               clinic_name: str, clinic_address: str,
                               recipient_email: str, sender_account: str,
                               gmail_kind: str, gmail_id: str, thread_id: str = "",
                               message_id: str = "", status: str,
                               operation_id: str = "") -> bool:
        """Insert one already-observed Gmail event idempotently, keyed by Clinic ID.

        This is an explicit write adapter; the dry-run planner never calls it. A
        different Clinic ID or conflicting Gmail ID is an error/HOLD, never merged.
        """
        import hashlib
        import uuid as uuid_module
        if status not in {"DRAFT", "SCHEDULED", "SENT", "REPLIED", "TRASHED", "UNKNOWN", "REVIEW"}:
            raise ValueError("Invalid Gmail history status")
        if not int(clinic_id) or not gmail_id.strip() or gmail_kind not in {"draft", "message"}:
            raise ValueError("Clinic ID and one observed Draft/Message ID are required")
        key = f"gmail-history-v1|{int(clinic_id)}|{gmail_kind}|{gmail_id.strip()}"
        idem = hashlib.sha256(key.encode()).hexdigest()
        stable_operation_id = str(uuid_module.uuid5(uuid_module.NAMESPACE_URL, key))
        if operation_id and operation_id != stable_operation_id:
            raise ValueError("Backfill operation ID must be stable for the Gmail event")
        operation_id = stable_operation_id
        outreach_id = stable_operation_id
        batch_id = str(uuid_module.uuid5(uuid_module.NAMESPACE_URL, "batch|" + key))
        campaign_id = f"gmail-history:{gmail_kind}:{hashlib.sha256(gmail_id.encode()).hexdigest()[:24]}"
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO outreach.clinic_draft_guard
                    (clinic_id,first_campaign_id,first_outreach_id)
                    VALUES (%s,%s,%s) ON CONFLICT (clinic_id) DO NOTHING""",
                    (int(clinic_id), campaign_id, outreach_id),
                )
                cur.fetchone()
                cur.execute(
                    f"""INSERT INTO {TABLE}
                    (outreach_id,operation_id,idempotency_key,clinic_id,clinic_uuid,
                     clinic_name_snapshot,clinic_address_snapshot,recipient_email,sender_account,
                     staff_name,campaign_id,generation_batch_id,status,listing_status,article_type,
                     subject,body_sha256,gmail_draft_id,gmail_message_id,gmail_thread_id,last_error)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'UNCONFIRMED','UNKNOWN',
                     %s,%s,%s,%s,%s,'READ-ONLY source verified by operator')
                    ON CONFLICT DO NOTHING RETURNING outreach_id""",
                    (outreach_id, operation_id, idem, int(clinic_id), clinic_uuid or None,
                     clinic_name, clinic_address or "", recipient_email or "", sender_account,
                     "Gmail history backfill", campaign_id, batch_id, status,
                     f"Historical Gmail {gmail_kind}", hashlib.sha256(key.encode()).hexdigest(),
                     gmail_id if gmail_kind == "draft" else None,
                     message_id or (gmail_id if gmail_kind == "message" else None), thread_id or None),
                )
                inserted = cur.fetchone() is not None
                if not inserted:
                    cur.execute(
                        f"""SELECT clinic_id,status,gmail_draft_id,gmail_message_id,gmail_thread_id
                        FROM {TABLE} WHERE idempotency_key=%s OR gmail_draft_id=%s OR gmail_message_id=%s""",
                        (idem, gmail_id if gmail_kind == "draft" else None,
                         gmail_id if gmail_kind == "message" else None),
                    )
                    rows = cur.fetchall()
                    expected_id = str(clinic_id)
                    if len(rows) != 1 or str(rows[0][0]) != expected_id:
                        raise OutreachDatabaseError("Gmail history ID collision requires HOLD")
                    row = rows[0]
                    if (str(row[1]) != status or (row[2] or "") != (gmail_id if gmail_kind == "draft" else "")
                            or (row[3] or "") != (message_id or (gmail_id if gmail_kind == "message" else ""))
                            or (row[4] or "") != (thread_id or "")):
                        raise OutreachDatabaseError("Existing Gmail history differs; requires HOLD")
        return inserted


__all__ = ["OutreachDatabaseError", "OutreachDeliveryRepository", "DraftReservation",
           "connect_outreach_database", "BLOCKING_STATUSES"]
