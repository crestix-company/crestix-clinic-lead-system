from __future__ import annotations

from pathlib import Path

from src.repository.outreach_delivery_repository import (
    OUTREACH_ROLE, RUNTIME_ROLE, TABLE, _idempotency_key,
)


MIGRATION = Path("supabase/migrations/20261016172806_outreach_email_delivery_records.sql")


def test_outreach_ledger_migration_is_separate_rls_protected_and_has_no_delete_grant():
    sql = MIGRATION.read_text(encoding="utf-8").casefold()
    assert "create table outreach.email_delivery_records" in sql
    assert "clinic_id bigint not null references public.clinics(id) on delete restrict" in sql
    assert "unique (clinic_id, campaign_id)" in sql
    assert "operation_id uuid not null unique" in sql
    assert "gmail_draft_id" in sql and "gmail_message_id" in sql and "gmail_thread_id" in sql
    assert "enable row level security" in sql and "force row level security" in sql
    assert "grant select, insert, update on outreach.email_delivery_records to clinic_outreach_runtime" in sql
    assert "revoke all on outreach.email_delivery_records from public, anon, authenticated, clinic_runtime" in sql
    assert "grant clinic_outreach_runtime to clinic_runtime" in sql
    assert "delete" not in sql.split("grant select, insert, update on outreach.email_delivery_records")[1].split(";")[0]
    assert TABLE in sql and OUTREACH_ROLE in sql and RUNTIME_ROLE in sql


def test_idempotency_key_is_stable_per_clinic_and_campaign_only():
    assert _idempotency_key(17, "campaign-a") == _idempotency_key(17, " campaign-a ")
    assert _idempotency_key(17, "campaign-a") != _idempotency_key(18, "campaign-a")
    assert _idempotency_key(17, "campaign-a") != _idempotency_key(17, "campaign-b")
