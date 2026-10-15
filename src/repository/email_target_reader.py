"""Read-only producer for the Email sidecar's verified-HP target snapshots."""
from __future__ import annotations

import os


EMAIL_TARGET_READER_DB_URL_ENV_VAR = "EMAIL_TARGET_READER_DB_URL"

# Pooled connections (Supabase pgbouncer/Supavisor) do not reliably honor
# session-level `-c default_transaction_read_only=on` or `SET` -- state can
# fail to persist even within a single uncommitted transaction. Read-only is
# therefore guaranteed at the role/grant level instead: the connecting role
# must have SELECT on exactly the tables this reader touches and must not
# hold INSERT/UPDATE/DELETE/TRUNCATE on them. Fail closed if either check
# can't be confirmed.
REQUIRED_SELECT_TABLES = ("public.clinics", "hp_research.clinic_hp_research")
FORBIDDEN_WRITE_PRIVILEGES = ("INSERT", "UPDATE", "DELETE", "TRUNCATE")


def _verify_read_only_role(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT current_user")
        current_user = str(cur.fetchone()[0] or "").strip().casefold()
        if current_user != "clinic_email_target_reader":
            raise RuntimeError(
                "Email Target Reader接続ロールがclinic_email_target_readerではありません。"
            )
        for table in REQUIRED_SELECT_TABLES:
            cur.execute("SELECT has_table_privilege(current_user, %s, 'SELECT')", (table,))
            if not cur.fetchone()[0]:
                raise RuntimeError(f"Clinic/HP Target Readerに{table}のSELECT権限がありません。")
            for privilege in FORBIDDEN_WRITE_PRIVILEGES:
                cur.execute("SELECT has_table_privilege(current_user, %s, %s)", (table, privilege))
                if cur.fetchone()[0]:
                    raise RuntimeError(
                        f"Clinic/HP Target Readerの接続ロールが{table}に{privilege}権限を持っています。"
                        "READ ONLY専用ロールを使用してください。"
                    )
    conn.commit()


class SupabaseEmailTargetReader:
    """The connection's role is verified to hold only SELECT on the tables this reader touches."""

    def __init__(self, conn):
        self._conn = conn

    def eligible_targets(self, *, limit=None):
        params = () if limit is None else (max(0, int(limit)),)
        limit_sql = "" if limit is None else " LIMIT %s"
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT c.id,c.uuid,c.clinic_name,c.prefecture,c.address,c.phone,
                      COALESCE(NULLIF(BTRIM(c.hp_url),''),
                        CASE WHEN c.maps_presence_status='MAPS_MATCHED_WEBSITE'
                          THEN NULLIF(BTRIM(c.maps_website_url),'') ELSE NULL END) AS official_hp_url,
                      r.researched_at
                    FROM public.clinics c
                    LEFT JOIN hp_research.clinic_hp_research r ON r.clinic_id=c.id
                    WHERE c.active=true AND c.merged_into IS NULL AND c.merge_hold=false
                      AND COALESCE(BTRIM(c.exclude_reason),'')=''
                      AND c.hp_status='VERIFIED'
                      AND (COALESCE(BTRIM(c.hp_url),'')<>'' OR
                           (c.maps_presence_status='MAPS_MATCHED_WEBSITE' AND
                            COALESCE(BTRIM(c.maps_website_url),'')<>''))
                    ORDER BY c.id
                    """ + limit_sql,
                    params,
                )
                columns = [item.name for item in cur.description]
                rows = [dict(zip(columns, row)) for row in cur.fetchall()]
            self._conn.commit()
            from src.normalizer.address import extract_municipality
            for row in rows:
                row["clinic_id"] = int(row.pop("id"))
                row["clinic_uuid"] = str(row.pop("uuid") or "").strip()
                row["official_hp_url"] = str(row.get("official_hp_url") or "").strip()
                row["hp_verified_at"] = row.pop("researched_at", None)
                row["city"] = extract_municipality(str(row.get("address") or "")) or ""
            return [row for row in rows if row["clinic_uuid"] and row["official_hp_url"]]
        except Exception:
            self._conn.rollback()
            raise


def connect_email_target_reader(*, environ=None, connect_fn=None):
    """Open a dedicated read-only Clinic/HP connection; never use Email/runtime fallback."""
    env = os.environ if environ is None else environ
    from src.repository.email_database import email_database_status, email_target_reader_status
    safety = email_database_status(env)
    if not safety.ready:
        raise RuntimeError(safety.message)
    source_safety = email_target_reader_status(env)
    if not source_safety.ready:
        raise RuntimeError(source_safety.message)
    url = (env.get(EMAIL_TARGET_READER_DB_URL_ENV_VAR) or "").strip()
    if not url:
        raise RuntimeError("Email Target Reader接続が未設定です。")
    if connect_fn is None:
        import psycopg
        conn = psycopg.connect(url, connect_timeout=20)
    else:
        conn = connect_fn(url, read_only=True)
    try:
        _verify_read_only_role(conn)
    except Exception:
        conn.close()
        raise
    return SupabaseEmailTargetReader(conn)
