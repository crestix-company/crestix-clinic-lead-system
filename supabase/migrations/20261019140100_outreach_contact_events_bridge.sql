-- Additive only. Bridges the current-state ledger (outreach.email_delivery_records,
-- created by 20261016172806_...sql) into the EXISTING, already-manually-created
-- append-only event log (outreach.email_contact_events) that
-- outreach.clinic_email_summary already reads from. Neither existing table's shape,
-- data, RLS, or grants are changed by this file -- this only adds a function and a
-- trigger on email_delivery_records.
--
-- Idempotency / no double-counting: outreach.email_contact_events.event_key already
-- has a UNIQUE constraint (pre-existing, not created here). This trigger derives a
-- deterministic key from (outreach_id, event_type), so re-syncing the exact same
-- transition (e.g. re-running Gmail-state reconciliation against an already-SENT
-- row) inserts the same event_key again, and ON CONFLICT DO NOTHING makes that a
-- no-op. sent_count in outreach.clinic_email_summary can therefore never
-- double-count a transition that already happened once, however many times the
-- same Gmail state is resynced.
--
-- "Sent count only increases on Gmail confirmation": the trigger only emits a SENT
-- event when status is being SET to SENT (an actual transition), never on an
-- unrelated column-only update (e.g. a last_synced_at touch while status stays the
-- same), and the application only ever sets status='SENT' after Gmail's own API
-- confirms a sent message (see src/outreach/gmail_workflow.py's
-- _reconcile_existing_ledger SENT branch) -- never speculatively.
--
-- SECURITY DEFINER is required: clinic_outreach_runtime (the role that writes
-- email_delivery_records) has zero grants on email_contact_events today (confirmed:
-- only the table owner has any privilege on it). The function therefore runs as its
-- owner (the migration-applying role), so it can insert into the event log without
-- widening clinic_outreach_runtime's own privileges on that table.
--
-- Must run AFTER 20261016172806_outreach_email_delivery_records.sql.
-- Verified locally (initdb + psql): draft->sent->resync-same-sent produces exactly
-- one SENT event and sent_count=1 after both the first and the resynced update;
-- a second campaign for the same clinic produces a second, independent event
-- without inflating the first; the (clinic_id, campaign_id) unique constraint from
-- the base migration still rejects a duplicate reservation unchanged.

begin;

create or replace function outreach.record_contact_event()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, outreach
as $$
declare
  v_event_type text;
  v_key text;
begin
  v_event_type := case new.status
    when 'DRAFT' then 'DRAFT_CREATED'
    when 'SENT' then 'SENT'
    when 'SCHEDULED' then 'SCHEDULED'
    when 'REPLIED' then 'REPLIED'
    when 'TRASHED' then 'TRASHED'
    when 'REVIEW' then 'HOLD'
    when 'UNKNOWN' then 'HOLD'
    else null
  end;
  if v_event_type is null then
    return new;
  end if;
  -- Only on an actual transition into this status; a column-only update that
  -- leaves status unchanged must never emit a second event for the same state.
  if tg_op = 'UPDATE' and old.status = new.status then
    return new;
  end if;
  v_key := new.outreach_id::text || ':' || v_event_type;
  insert into outreach.email_contact_events
    (clinic_id, campaign_id, event_key, event_type, gmail_message_id, gmail_thread_id,
     gmail_draft_id, occurred_at)
  values
    (new.clinic_id, new.campaign_id, v_key, v_event_type, new.gmail_message_id,
     new.gmail_thread_id, new.gmail_draft_id, coalesce(new.sent_at, new.updated_at, now()))
  on conflict (event_key) do nothing;
  return new;
end;
$$;
-- CREATE FUNCTION made the migration-applying role (the email_contact_events table
-- owner) this function's owner; SECURITY DEFINER means it runs with that owner's
-- privileges regardless of which role's DML on email_delivery_records fires it.

drop trigger if exists email_delivery_records_record_contact_event
  on outreach.email_delivery_records;
create trigger email_delivery_records_record_contact_event
after insert or update on outreach.email_delivery_records
for each row execute function outreach.record_contact_event();

commit;
