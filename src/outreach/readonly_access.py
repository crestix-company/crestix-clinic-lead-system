"""READ ONLY Clinic Master access for the outreach Canary.

Uses the dedicated read-only role URL (CLINIC_READONLY_DB_URL, the same variable as the Email
target reader), never the read/write runtime URL. Fails closed unless the role holds SELECT and no
INSERT/UPDATE/DELETE/TRUNCATE on every table the Clinic query path reads.
"""
from __future__ import annotations

import os

CLINIC_READONLY_DB_URL_ENV_VAR = "CLINIC_READONLY_DB_URL"
# Tables read by SupabaseClinicRepository.query()/_batch_get() and supabase_filters.
READ_TABLES = (
    "public.clinics",
    "hp_research.clinic_hp_research",
    "treatment.clinic_treatment_research",
    "treatment.clinic_research_status",
)
FORBIDDEN_WRITE_PRIVILEGES = ("INSERT", "UPDATE", "DELETE", "TRUNCATE")


class ReadOnlyAccessError(RuntimeError):
    """Access is missing or not provably read-only; the caller must stop."""


def required_permissions_text() -> str:
    tables = ", ".join(READ_TABLES)
    return (
        f"環境変数 {CLINIC_READONLY_DB_URL_ENV_VAR} に、READ ONLY専用ロールの接続URLを設定してください。"
        f"そのロールには次のテーブルのSELECT権限のみが必要です（INSERT/UPDATE/DELETE/TRUNCATE権限は不可）: {tables}"
    )


def readonly_url(environ=None) -> str:
    env = os.environ if environ is None else environ
    url = (env.get(CLINIC_READONLY_DB_URL_ENV_VAR) or "").strip()
    if not url:
        raise ReadOnlyAccessError(f"{CLINIC_READONLY_DB_URL_ENV_VAR} が未設定です。" + required_permissions_text())
    runtime = (env.get("SUPABASE_RUNTIME_DB_URL") or "").strip()
    if runtime and runtime == url:
        raise ReadOnlyAccessError("READ ONLY URLが書き込み可能なruntime URLと同一です。" + required_permissions_text())
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
        raise ReadOnlyAccessError("READ ONLY権限を確認できません: " + " / ".join(problems)
                                  + "。" + required_permissions_text())


def open_readonly_clinic_repository(environ=None, connect_fn=None):
    """Returns (repository, conn). Closes the connection and raises if not provably read-only."""
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
        from src.repository.supabase_adapter import SupabaseClinicRepository, SupabaseHpResearchRepository
        return SupabaseClinicRepository(conn, SupabaseHpResearchRepository(conn)), conn
    except Exception:
        conn.close()
        raise
