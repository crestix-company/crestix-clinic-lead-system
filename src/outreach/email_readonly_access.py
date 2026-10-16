"""READ ONLY Email Research access for the outreach quality verification.

This is an independent Gate from src.outreach.readonly_access (Clinic Master). It never
reuses that Gate's env var, connection, or role, and it never touches the Production
runtime role (clinic_email_runtime) which holds write privileges. Fails closed unless the
configured role holds SELECT and no INSERT/UPDATE/DELETE/TRUNCATE on every table/view this
reader uses.

No write method is implemented anywhere in this module.
"""
from __future__ import annotations

import os

EMAIL_READONLY_DB_URL_ENV_VAR = "EMAIL_READONLY_DB_URL"
# Tables/view read by EmailReadOnlyReader.primary_emails_by_clinic_id().
READ_TABLES = (
    "email_research.clinic_email_status",
    "email_research.email_candidates",
    "email_research.clinic_targets",
)
FORBIDDEN_WRITE_PRIVILEGES = ("INSERT", "UPDATE", "DELETE", "TRUNCATE")


class EmailReadOnlyAccessError(RuntimeError):
    """Access is missing or not provably read-only; the caller must stop."""


def required_permissions_text() -> str:
    tables = ", ".join(READ_TABLES)
    return (
        f"環境変数 {EMAIL_READONLY_DB_URL_ENV_VAR} に、READ ONLY専用ロールの接続URLを設定してください。"
        f"そのロールには次のテーブル/ビューのSELECT権限のみが必要です（INSERT/UPDATE/DELETE/TRUNCATE権限は不可）: {tables}"
    )


def readonly_url(environ=None) -> str:
    env = os.environ if environ is None else environ
    url = (env.get(EMAIL_READONLY_DB_URL_ENV_VAR) or "").strip()
    if not url:
        raise EmailReadOnlyAccessError(
            f"{EMAIL_READONLY_DB_URL_ENV_VAR} が未設定です。安全に停止します。" + required_permissions_text())
    runtime = (env.get("SUPABASE_RUNTIME_DB_URL") or "").strip()
    if runtime and runtime == url:
        raise EmailReadOnlyAccessError("READ ONLY URLが書き込み可能なruntime URLと同一です。" + required_permissions_text())
    return url


def verify_read_only_role(conn) -> None:
    problems = []
    with conn.cursor() as cur:
        for table in READ_TABLES:
            cur.execute("SELECT to_regclass(%s) IS NOT NULL", (table,))
            if not cur.fetchone()[0]:
                problems.append(f"{table}: 参照できません（存在しない、またはスキーマのUSAGE権限なし）")
                continue
            cur.execute("SELECT has_table_privilege(current_user, %s, 'SELECT')", (table,))
            if not cur.fetchone()[0]:
                problems.append(f"{table}: SELECT権限なし")
            for privilege in FORBIDDEN_WRITE_PRIVILEGES:
                cur.execute("SELECT has_table_privilege(current_user, %s, %s)", (table, privilege))
                if cur.fetchone()[0]:
                    problems.append(f"{table}: {privilege}権限あり（READ ONLY専用ロールではない）")
    if problems:
        raise EmailReadOnlyAccessError("READ ONLY権限を確認できません: " + " / ".join(problems)
                                       + "。" + required_permissions_text())


class EmailReadOnlyReader:
    """SELECT専用。email_research.*への書き込みメソッドは実装しない。"""

    def __init__(self, conn):
        self._conn = conn

    def primary_emails_by_clinic_id(self, clinic_ids: list[int]) -> dict[int, str]:
        """clinic_id -> Primary Emailのみ返す（Clinic UUIDへの変換は呼び出し側でpublic.clinics経由で行う）。

        既存のHuman Review優先ロジック・final_sales_eligibleの判定（src.master.email_sales_eligibility）
        はPrimary選定時点（email_research側のset_primary_candidate）で既に適用済みという前提の上で、
        このSELECTでも final_sales_eligible = true を明示的に要求し、未承認・低信頼・除外対象
        （final_sales_eligibleがtrue以外）の候補を一切返さない。
        """
        ids = [int(cid) for cid in clinic_ids]
        if not ids:
            return {}
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.clinic_id, p.email
                FROM email_research.clinic_email_status s
                JOIN email_research.email_candidates p
                  ON p.clinic_id = s.clinic_id AND p.candidate_id = s.primary_email_candidate_id
                WHERE s.primary_email_candidate_id IS NOT NULL
                  AND s.clinic_id = ANY(%s::bigint[])
                  AND p.final_sales_eligible = true
                """,
                (ids,),
            )
            rows = cur.fetchall()
        return {int(clinic_id): str(email or "").strip() for clinic_id, email in rows}

    def primary_candidates_by_clinic_id(self, clinic_ids: list[int]) -> dict[int, dict[str, str]]:
        """Return the approved Primary Email plus its immutable Candidate provenance id."""
        ids = [int(cid) for cid in clinic_ids]
        if not ids:
            return {}
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.clinic_id, p.candidate_id, p.email
                FROM email_research.clinic_email_status s
                JOIN email_research.email_candidates p
                  ON p.clinic_id = s.clinic_id AND p.candidate_id = s.primary_email_candidate_id
                WHERE s.primary_email_candidate_id IS NOT NULL
                  AND s.clinic_id = ANY(%s::bigint[])
                  AND p.final_sales_eligible = true
                  AND cardinality(p.hard_block_codes) = 0
                """,
                (ids,),
            )
            rows = cur.fetchall()
        return {
            int(clinic_id): {"candidate_id": str(candidate_id), "email": str(email or "").strip()}
            for clinic_id, candidate_id, email in rows
        }


def open_readonly_email_reader(environ=None, connect_fn=None):
    """Returns (reader, conn). Closes the connection and raises if not provably read-only."""
    url = readonly_url(environ)
    if connect_fn is None:
        from src.repository.supabase_adapter import connect
        conn = connect(url, autocommit=True)
    else:
        conn = connect_fn(url)
    try:
        verify_read_only_role(conn)
        with conn.cursor() as cur:  # belt and braces; the role check above is the real guarantee
            cur.execute("SET default_transaction_read_only = on")
        return EmailReadOnlyReader(conn), conn
    except Exception:
        conn.close()
        raise


def read_primary_emails_readonly(clinic_ids: list[int], *, environ=None, connect_fn=None) -> dict[int, str]:
    """EMAIL_READONLY_DB_URLが未設定、または権限が確認できない場合は安全停止（EmailReadOnlyAccessError）する。"""
    reader, conn = open_readonly_email_reader(environ, connect_fn)
    try:
        return reader.primary_emails_by_clinic_id(clinic_ids)
    finally:
        conn.close()
