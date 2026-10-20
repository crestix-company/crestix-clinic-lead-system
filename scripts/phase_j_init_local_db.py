"""Initialize the disposable Phase J PostgreSQL cluster and apply local-only migrations."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import psycopg

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = "/private/tmp/clinic_phase_j_pgdata_20261020"
SOCKET_DIR = "/private/tmp/clinic_phase_j_socket_20261020"
PORT = 55439
DATABASE = "phase_j_outreach_import_v1"
USER = "maekawahiroyuki"
PSQL = "/opt/homebrew/bin/psql"
MIGRATIONS = (
    "20261016172806_outreach_email_delivery_records.sql",
    "20261019140000_outreach_email_delivery_records_trashed_status.sql",
    "20261019140100_outreach_contact_events_bridge.sql",
    "20261019140200_outreach_clinic_email_summary_grant.sql",
    "20261019150000_gmail_history_backfill_support.sql",
    "20261019151000_clinic_draft_guard.sql",
    "20261019160000_outreach_full_sheets_gmail_integration.sql",
    "20261020100000_shared_gas_clinic_draft_api.sql",
    "20261020130000_gmail_message_trash_status.sql",
)


def dsn(dbname: str) -> str:
    return f"host={SOCKET_DIR} port={PORT} dbname={dbname} user={USER}"


def verify_cluster(conn: psycopg.Connection) -> None:
    name, directory, address = conn.execute(
        "select current_database(),current_setting('data_directory'),inet_server_addr()::text"
    ).fetchone()
    if directory != DATA_DIR or address is not None:
        raise RuntimeError("Safety stop: PostgreSQL is not the Phase J isolated Unix-socket cluster")
    if name not in {"postgres", DATABASE}:
        raise RuntimeError("Safety stop: unexpected database name")


def initialize() -> dict[str, object]:
    with psycopg.connect(dsn("postgres"), autocommit=True, connect_timeout=3) as conn:
        verify_cluster(conn)
        exists = conn.execute("select 1 from pg_database where datname=%s", (DATABASE,)).fetchone()
        if exists:
            raise RuntimeError(f"{DATABASE} already exists; refusing to reinitialize or alter an existing local database")
        conn.execute("""do $$ begin
          if not exists(select 1 from pg_roles where rolname='anon') then create role anon nologin; end if;
          if not exists(select 1 from pg_roles where rolname='authenticated') then create role authenticated nologin; end if;
          if not exists(select 1 from pg_roles where rolname='clinic_runtime') then create role clinic_runtime nologin; end if;
        end $$""")
        conn.execute(f'create database "{DATABASE}"')

    with psycopg.connect(dsn(DATABASE), autocommit=True, connect_timeout=3) as conn:
        verify_cluster(conn)
        conn.execute("create extension if not exists pgcrypto")
        conn.execute("""create table public.clinics(
          id bigint primary key,uuid text not null,clinic_name text not null default '',
          address text not null default '',phone text not null default '',active boolean not null default true,
          merged_into bigint,merge_hold boolean not null default false)""")
        conn.execute("create schema outreach")
        conn.execute("""create table outreach.email_contact_events(
          event_id bigint generated always as identity primary key,
          clinic_id bigint not null references public.clinics(id),campaign_id text not null default '',
          event_key text not null unique,event_type text not null,gmail_message_id text,gmail_thread_id text,
          gmail_draft_id text,occurred_at timestamptz,created_at timestamptz not null default now())""")
        conn.execute("alter table outreach.email_contact_events enable row level security")
        conn.execute("alter table outreach.email_contact_events force row level security")
        conn.execute("""create view outreach.clinic_email_summary as
          select clinic_id,count(*) filter(where event_type='DRAFT_CREATED') as draft_count,
                 count(*) filter(where event_type='SENT') as sent_count,
                 count(*) filter(where event_type='REPLIED') as replied_count
          from outreach.email_contact_events group by clinic_id""")

    for migration in MIGRATIONS:
        path = ROOT / "supabase" / "migrations" / migration
        if not path.is_file():
            raise FileNotFoundError(path)
        subprocess.run([PSQL, dsn(DATABASE), "-v", "ON_ERROR_STOP=1", "-f", str(path)],
                       check=True, cwd=ROOT)

    with psycopg.connect(dsn(DATABASE), connect_timeout=3) as conn:
        verify_cluster(conn)
        table_rows = conn.execute("""select count(*) from information_schema.tables
          where table_schema='outreach' and table_type='BASE TABLE'""").fetchone()[0]
        trash_check = conn.execute("""select pg_get_constraintdef(oid) from pg_constraint
          where conrelid='outreach.gmail_messages'::regclass and conname='gmail_messages_message_status_check'""").fetchone()[0]
        rls_ok = conn.execute("""select bool_and(c.relrowsecurity and c.relforcerowsecurity)
          from pg_class c join pg_namespace n on n.oid=c.relnamespace
          where n.nspname='outreach' and c.relkind='r' and c.relname in
          ('sales_cases','source_import_batches','source_import_rows','gmail_messages','email_state_history',
           'article_selection_results','engagement_events','calendar_appointments','sync_jobs')""").fetchone()[0]
        if "TRASH" not in trash_check or "CANCELLED" not in trash_check or not rls_ok:
            raise RuntimeError("Local migration verification failed (TRASH/CANCELLED or RLS)")
        return {"database": DATABASE, "data_directory": DATA_DIR, "outreach_base_tables": table_rows,
                "gmail_status_constraint": trash_check, "rls_force_verified": rls_ok,
                "migrations_applied": list(MIGRATIONS), "target": "local isolated PostgreSQL only"}


if __name__ == "__main__":
    try:
        import json
        print(json.dumps(initialize(), ensure_ascii=False, indent=2))
    except Exception as exc:
        print(f"Phase J local DB setup stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
