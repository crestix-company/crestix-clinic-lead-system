-- Outreach delivery ledger. Prepare only; Production application requires a separately
-- approved change window. The UI connects as clinic_runtime and SET ROLEs to the
-- restricted clinic_outreach_runtime role before accessing this table.

begin;

create schema if not exists outreach;

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'clinic_outreach_runtime') then
    create role clinic_outreach_runtime nologin nosuperuser nocreatedb nocreaterole
      noreplication nobypassrls;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'clinic_runtime') then
    raise exception 'clinic_runtime role must exist before applying outreach ledger migration';
  end if;
end $$;

create table outreach.email_delivery_records (
  outreach_id uuid primary key default gen_random_uuid(),
  operation_id uuid not null unique,
  idempotency_key text not null unique,
  clinic_id bigint not null references public.clinics(id) on delete restrict,
  clinic_uuid text not null,
  clinic_name_snapshot text not null,
  clinic_address_snapshot text not null default '',
  recipient_email text not null,
  sender_account text not null,
  staff_name text not null,
  campaign_id text not null,
  generation_batch_id uuid not null,
  gmail_draft_id text,
  gmail_message_id text,
  gmail_thread_id text,
  status text not null check (status in
    ('DRAFT','SENT','SCHEDULED','REPLIED','FAILED','UNKNOWN','REVIEW')),
  listing_status text not null check (listing_status in
    ('LISTED','NOT_LISTED','UNCONFIRMED','ERROR')),
  article_type text not null check (article_type in
    ('NORMAL','PR','SUPERVISED','UNKNOWN')),
  example_article_url text not null default '',
  listing_article_url text not null default '',
  clinic_profile_url text not null default '',
  subject text not null,
  body_sha256 text not null,
  scheduled_at timestamptz,
  sent_at timestamptz,
  last_synced_at timestamptz,
  last_error text not null default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (clinic_id, campaign_id)
);

create unique index email_delivery_records_gmail_draft_id_uq
  on outreach.email_delivery_records (gmail_draft_id) where gmail_draft_id is not null;
create unique index email_delivery_records_gmail_message_id_uq
  on outreach.email_delivery_records (gmail_message_id) where gmail_message_id is not null;
create index email_delivery_records_campaign_status_idx
  on outreach.email_delivery_records (campaign_id, status, clinic_id);
create index email_delivery_records_clinic_uuid_idx
  on outreach.email_delivery_records (clinic_uuid, created_at desc);

create or replace function outreach.touch_email_delivery_updated_at()
returns trigger
language plpgsql
set search_path = pg_catalog, outreach
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

create trigger email_delivery_records_touch_updated_at
before update on outreach.email_delivery_records
for each row execute function outreach.touch_email_delivery_updated_at();

alter table outreach.email_delivery_records enable row level security;
alter table outreach.email_delivery_records force row level security;

revoke all on schema outreach from public, anon, authenticated;
revoke all on schema outreach from clinic_runtime;
revoke all on outreach.email_delivery_records from public, anon, authenticated, clinic_runtime;
grant usage on schema outreach to clinic_outreach_runtime;
grant select on outreach.email_delivery_records to clinic_outreach_runtime;
grant insert (outreach_id, operation_id, idempotency_key, clinic_id, clinic_uuid,
  clinic_name_snapshot, clinic_address_snapshot, recipient_email, sender_account, staff_name,
  campaign_id, generation_batch_id, status, listing_status, article_type,
  example_article_url, listing_article_url, clinic_profile_url, subject, body_sha256, last_error)
  on outreach.email_delivery_records to clinic_outreach_runtime;
grant update (status, gmail_draft_id, gmail_message_id, gmail_thread_id,
  sent_at, scheduled_at, last_synced_at, last_error)
  on outreach.email_delivery_records to clinic_outreach_runtime;

drop policy if exists clinic_outreach_runtime_email_delivery_select on outreach.email_delivery_records;
create policy clinic_outreach_runtime_email_delivery_select
  on outreach.email_delivery_records for select to clinic_outreach_runtime using (true);
drop policy if exists clinic_outreach_runtime_email_delivery_insert on outreach.email_delivery_records;
create policy clinic_outreach_runtime_email_delivery_insert
  on outreach.email_delivery_records for insert to clinic_outreach_runtime with check (true);
drop policy if exists clinic_outreach_runtime_email_delivery_update on outreach.email_delivery_records;
create policy clinic_outreach_runtime_email_delivery_update
  on outreach.email_delivery_records for update to clinic_outreach_runtime
  using (true) with check (true);

grant clinic_outreach_runtime to clinic_runtime;

commit;
