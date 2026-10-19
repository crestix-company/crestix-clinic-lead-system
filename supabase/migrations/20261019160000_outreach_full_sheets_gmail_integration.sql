-- Additive proposal for Clinic Lead outreach and source-data integration.
-- Prerequisite: 20261016172806_outreach_email_delivery_records.sql.
-- This file is intentionally not applied to any remote database in this task.

begin;

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'clinic_outreach_runtime') then
    raise exception 'clinic_outreach_runtime role must exist before this migration';
  end if;
end $$;

create table outreach.sales_cases (
  sales_case_id uuid primary key default gen_random_uuid(),
  operation_key text not null unique,
  source_system text not null check (source_system in ('GAS_SHEETS','CSV_IMPORT','CLINIC_LEAD','GMAIL_BACKFILL')),
  external_case_id text not null default '',
  clinic_id bigint not null references public.clinics(id) on delete restrict,
  clinic_uuid text not null,
  campaign_id text not null,
  is_owner text not null default '',
  fs_owner text not null default '',
  case_status text not null default 'OPEN' check (case_status in ('OPEN','ON_HOLD','CLOSED','CONFLICT')),
  source_updated_at timestamptz,
  imported_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  source_payload_hash text not null default '',
  conflict_status text not null default 'NONE' check (conflict_status in ('NONE','PENDING_REVIEW','RESOLVED'))
);
create unique index sales_cases_external_id_uq
  on outreach.sales_cases(source_system, external_case_id) where external_case_id <> '';
create index sales_cases_clinic_history_idx on outreach.sales_cases(clinic_id, created_at desc);
create index sales_cases_campaign_status_idx on outreach.sales_cases(campaign_id, case_status, updated_at desc);

-- Multiple distinct cases for one clinic are allowed, including within one campaign.
alter table outreach.email_delivery_records
  add column sales_case_id uuid references outreach.sales_cases(sales_case_id) on delete restrict;
grant insert (sales_case_id) on outreach.email_delivery_records to clinic_outreach_runtime;
insert into outreach.sales_cases
  (operation_key,source_system,external_case_id,clinic_id,clinic_uuid,campaign_id,
   is_owner,case_status,source_payload_hash)
select 'legacy-outreach:' || operation_id::text,'CLINIC_LEAD',outreach_id::text,
       clinic_id,clinic_uuid,campaign_id,staff_name,'OPEN',body_sha256
from outreach.email_delivery_records on conflict (operation_key) do nothing;
update outreach.email_delivery_records d
set sales_case_id=c.sales_case_id
from outreach.sales_cases c
where c.source_system='CLINIC_LEAD' and c.external_case_id=d.outreach_id::text
  and d.sales_case_id is null;
create index email_delivery_records_sales_case_history_idx
  on outreach.email_delivery_records(sales_case_id,created_at desc);

create table outreach.email_draft_save_reviews (
  draft_review_id uuid primary key default gen_random_uuid(),
  outreach_id uuid not null references outreach.email_delivery_records(outreach_id) on delete restrict,
  sales_case_id uuid not null references outreach.sales_cases(sales_case_id) on delete restrict,
  reviewed_by text not null,
  reviewed_at timestamptz not null,
  checklist jsonb not null check (jsonb_typeof(checklist)='object'),
  protocol text not null,
  created_at timestamptz not null default now()
);
create index email_draft_save_reviews_case_time_idx
  on outreach.email_draft_save_reviews(sales_case_id,reviewed_at desc);

create table outreach.email_pre_send_reviews (
  review_id uuid primary key default gen_random_uuid(),
  outreach_id uuid not null references outreach.email_delivery_records(outreach_id) on delete restrict,
  sales_case_id uuid references outreach.sales_cases(sales_case_id) on delete restrict,
  reviewed_by text not null,
  reviewed_at timestamptz not null,
  i3_definition_status text not null check (i3_definition_status in ('UNCONFIRMED','CONFIRMED')),
  i3_definition_id text not null default '',
  i3_definition_url text not null default '',
  checklist jsonb not null check (jsonb_typeof(checklist)='object'),
  protocol text not null,
  created_at timestamptz not null default now(),
  check (i3_definition_status <> 'CONFIRMED' or (i3_definition_id <> '' and i3_definition_url like 'https://%'))
);
create index email_pre_send_reviews_case_time_idx on outreach.email_pre_send_reviews(sales_case_id, reviewed_at desc);

create table outreach.gmail_messages (
  gmail_record_id uuid primary key default gen_random_uuid(),
  sales_case_id uuid not null references outreach.sales_cases(sales_case_id) on delete restrict,
  operation_key text not null,
  gmail_draft_id text,
  gmail_message_id text,
  gmail_thread_id text,
  message_status text not null check (message_status in ('DRAFT','SCHEDULED','SENT','REPLIED','CANCELLED','UNKNOWN','CONFLICT')),
  recipient_snapshot text not null,
  subject_snapshot text not null default '',
  body_sha256 text not null default '',
  scheduled_at timestamptz,
  sent_at timestamptz,
  replied_at timestamptz,
  gmail_updated_at timestamptz,
  last_synced_at timestamptz,
  source_payload_hash text not null default '',
  created_at timestamptz not null default now(),
  check (message_status <> 'SCHEDULED' or scheduled_at is not null)
);
create unique index gmail_messages_operation_key_uq on outreach.gmail_messages(operation_key);
create unique index gmail_messages_draft_id_uq on outreach.gmail_messages(gmail_draft_id) where gmail_draft_id is not null;
create unique index gmail_messages_message_id_uq on outreach.gmail_messages(gmail_message_id) where gmail_message_id is not null;
create index gmail_messages_case_status_idx on outreach.gmail_messages(sales_case_id,message_status,created_at desc);

create table outreach.email_state_history (
  state_event_id uuid primary key default gen_random_uuid(),
  gmail_record_id uuid not null references outreach.gmail_messages(gmail_record_id) on delete restrict,
  previous_status text,
  next_status text not null,
  source_system text not null check (source_system in ('GMAIL_API','GAS_SHEETS','CSV_IMPORT','CLINIC_LEAD')),
  source_event_key text not null default '',
  occurred_at timestamptz,
  recorded_at timestamptz not null default now(),
  source_payload_hash text not null default '',
  conflict_status text not null default 'NONE' check (conflict_status in ('NONE','PENDING_REVIEW','RESOLVED'))
);
create unique index email_state_history_source_event_uq
  on outreach.email_state_history(source_system,source_event_key) where source_event_key <> '';
create index email_state_history_message_time_idx on outreach.email_state_history(gmail_record_id,recorded_at desc);

create table outreach.article_selection_results (
  article_selection_id uuid primary key default gen_random_uuid(),
  sales_case_id uuid not null references outreach.sales_cases(sales_case_id) on delete restrict,
  clinic_id bigint not null references public.clinics(id) on delete restrict,
  article_status text not null check (article_status in ('LISTED','NOT_LISTED','UNCONFIRMED')),
  article_type text not null default 'UNKNOWN',
  actual_article_url text not null default '',
  similar_article_url text not null default '',
  clinic_detail_url text not null default '',
  article_audit_id text not null default '',
  source_payload_hash text not null default '',
  selected_at timestamptz not null default now(),
  selected_by text not null default '',
  supersedes_selection_id uuid references outreach.article_selection_results(article_selection_id) on delete restrict,
  conflict_status text not null default 'NONE' check (conflict_status in ('NONE','PENDING_REVIEW','RESOLVED'))
);
create index article_selection_case_history_idx on outreach.article_selection_results(sales_case_id,selected_at desc);

create table outreach.engagement_events (
  event_id uuid primary key default gen_random_uuid(),
  sales_case_id uuid not null references outreach.sales_cases(sales_case_id) on delete restrict,
  gmail_record_id uuid references outreach.gmail_messages(gmail_record_id) on delete restrict,
  operation_key text not null,
  source_system text not null check (source_system in ('TRACKING_API','GAS_SHEETS','CSV_IMPORT')),
  source_event_key text not null,
  event_type text not null check (event_type in ('OPEN','CLICK','REPLY')),
  event_at timestamptz not null,
  url text not null default '',
  confidence text not null default 'UNKNOWN',
  privacy_metadata jsonb not null default '{}'::jsonb,
  source_payload_hash text not null default '',
  received_at timestamptz not null default now(),
  unique(source_system,source_event_key)
);
create index engagement_events_case_type_time_idx on outreach.engagement_events(sales_case_id,event_type,event_at desc);

create table outreach.calendar_appointments (
  appointment_id uuid primary key default gen_random_uuid(),
  sales_case_id uuid not null references outreach.sales_cases(sales_case_id) on delete restrict,
  provider text not null check (provider in ('GOOGLE_CALENDAR','GAS_SHEETS','CSV_IMPORT')),
  external_event_id text not null default '',
  appointment_status text not null check (appointment_status in ('PROPOSED','CONFIRMED','CANCELLED','COMPLETED','CONFLICT')),
  starts_at timestamptz,
  ends_at timestamptz,
  meet_url text not null default '',
  is_owner text not null default '',
  fs_owner text not null default '',
  source_payload_hash text not null default '',
  source_updated_at timestamptz,
  last_synced_at timestamptz,
  check (ends_at is null or starts_at is null or ends_at > starts_at)
);
create unique index calendar_appointments_external_event_uq
  on outreach.calendar_appointments(provider,external_event_id) where external_event_id <> '';
create index calendar_appointments_case_time_idx on outreach.calendar_appointments(sales_case_id,starts_at desc);

create table outreach.source_import_batches (
  import_batch_id uuid primary key default gen_random_uuid(),
  source_system text not null check (source_system in ('GOOGLE_SHEETS','CSV')),
  source_file_id text not null default '',
  source_file_name text not null,
  source_tab text not null default '',
  source_file_checksum text not null,
  source_revision text not null default '',
  raw_headers jsonb not null,
  import_status text not null default 'PREVIEW' check (import_status in ('PREVIEW','IMPORTED','PARTIAL','FAILED','CONFLICT')),
  total_source_rows integer not null check (total_source_rows >= 0),
  inserted_rows integer not null default 0 check (inserted_rows >= 0),
  error_rows integer not null default 0 check (error_rows >= 0),
  imported_by text not null default '',
  imported_at timestamptz not null default now(),
  mapping_version text not null default '',
  unique(source_system,source_file_checksum,source_tab,source_revision)
);
create index source_import_batches_time_idx on outreach.source_import_batches(imported_at desc);

create table outreach.source_import_rows (
  import_row_id uuid primary key default gen_random_uuid(),
  import_batch_id uuid not null references outreach.source_import_batches(import_batch_id) on delete restrict,
  source_tab text not null,
  source_row_number integer not null check (source_row_number >= 1),
  raw_headers jsonb not null,
  raw_values jsonb not null,
  raw_row_hash text not null,
  candidate_clinic_id bigint references public.clinics(id) on delete restrict,
  candidate_clinic_uuid text not null default '',
  candidate_case_id text not null default '',
  candidate_operation_key text not null default '',
  linked_sales_case_id uuid references outreach.sales_cases(sales_case_id) on delete restrict,
  mapping_status text not null default 'UNREVIEWED'
    check (mapping_status in ('UNREVIEWED','MATCHED','AMBIGUOUS','CONFLICT','UNMATCHED','APPROVED')),
  imported_at timestamptz not null default now(),
  unique(import_batch_id,source_tab,source_row_number)
);
create index source_import_rows_match_idx on outreach.source_import_rows(candidate_clinic_id,candidate_case_id,candidate_operation_key);
create index source_import_rows_batch_status_idx on outreach.source_import_rows(import_batch_id,mapping_status);

create table outreach.sync_jobs (
  sync_job_id uuid primary key default gen_random_uuid(),
  operation_key text not null unique,
  source_system text not null check (source_system in ('GOOGLE_SHEETS','GMAIL_API','GOOGLE_CALENDAR','TRACKING_API')),
  sync_mode text not null check (sync_mode in ('INITIAL_FULL','INCREMENTAL','RECONCILE')),
  cursor_before text not null default '',
  cursor_after text not null default '',
  job_status text not null default 'QUEUED' check (job_status in ('QUEUED','RUNNING','SUCCEEDED','PARTIAL','FAILED','CONFLICT')),
  rows_seen integer not null default 0,
  rows_inserted integer not null default 0,
  rows_updated integer not null default 0,
  rows_conflicted integer not null default 0,
  started_at timestamptz,
  finished_at timestamptz,
  created_at timestamptz not null default now()
);
create index sync_jobs_source_time_idx on outreach.sync_jobs(source_system,created_at desc);

create table outreach.sync_errors (
  sync_error_id uuid primary key default gen_random_uuid(),
  sync_job_id uuid not null references outreach.sync_jobs(sync_job_id) on delete restrict,
  source_tab text not null default '',
  source_row_number integer,
  source_record_key text not null default '',
  error_code text not null,
  error_message text not null default '',
  retryable boolean not null default false,
  resolution_status text not null default 'OPEN' check (resolution_status in ('OPEN','RESOLVED','IGNORED')),
  created_at timestamptz not null default now(),
  resolved_at timestamptz
);
create index sync_errors_open_idx on outreach.sync_errors(sync_job_id,resolution_status,created_at);

create table outreach.audit_events (
  audit_event_id uuid primary key default gen_random_uuid(),
  actor text not null,
  action text not null,
  entity_type text not null,
  entity_id text not null,
  operation_key text not null default '',
  before_hash text not null default '',
  after_hash text not null default '',
  source_system text not null default 'CLINIC_LEAD',
  occurred_at timestamptz not null default now()
);
create index outreach_audit_entity_time_idx on outreach.audit_events(entity_type,entity_id,occurred_at desc);

-- RLS is defense-in-depth; direct service DB access assumes this role. No anon/authenticated access.
do $$
declare
  table_name text;
begin
  foreach table_name in array array[
    'sales_cases','email_draft_save_reviews','email_pre_send_reviews','gmail_messages','email_state_history',
    'article_selection_results','engagement_events','calendar_appointments',
    'source_import_batches','source_import_rows','sync_jobs','sync_errors','audit_events'
  ] loop
    execute format('alter table outreach.%I enable row level security', table_name);
    execute format('alter table outreach.%I force row level security', table_name);
    execute format('revoke all on outreach.%I from public, anon, authenticated, clinic_runtime', table_name);
    execute format('grant select, insert on outreach.%I to clinic_outreach_runtime', table_name);
    execute format(
      'create policy %I on outreach.%I for all to clinic_outreach_runtime using (true) with check (true)',
      'clinic_outreach_runtime_' || table_name || '_access', table_name
    );
  end loop;
end $$;

grant update (is_owner,fs_owner,case_status,source_updated_at,updated_at,source_payload_hash,conflict_status)
  on outreach.sales_cases to clinic_outreach_runtime;
grant update (message_status,scheduled_at,sent_at,replied_at,gmail_updated_at,last_synced_at,source_payload_hash)
  on outreach.gmail_messages to clinic_outreach_runtime;
grant update (appointment_status,starts_at,ends_at,meet_url,is_owner,fs_owner,
  source_payload_hash,source_updated_at,last_synced_at)
  on outreach.calendar_appointments to clinic_outreach_runtime;
grant update (import_status,inserted_rows,error_rows) on outreach.source_import_batches to clinic_outreach_runtime;
grant update (candidate_clinic_id,candidate_clinic_uuid,candidate_case_id,candidate_operation_key,
  linked_sales_case_id,mapping_status) on outreach.source_import_rows to clinic_outreach_runtime;
grant update (cursor_after,job_status,rows_seen,rows_inserted,rows_updated,rows_conflicted,started_at,finished_at)
  on outreach.sync_jobs to clinic_outreach_runtime;
grant update (resolution_status,resolved_at) on outreach.sync_errors to clinic_outreach_runtime;

-- Append-only review, state, event, article, and audit logs; imported raw row values cannot be updated.
revoke update,delete,truncate on outreach.email_pre_send_reviews from clinic_outreach_runtime;
revoke update,delete,truncate on outreach.email_draft_save_reviews from clinic_outreach_runtime;
revoke update,delete,truncate on outreach.email_state_history from clinic_outreach_runtime;
revoke update,delete,truncate on outreach.engagement_events from clinic_outreach_runtime;
revoke update,delete,truncate on outreach.article_selection_results from clinic_outreach_runtime;
revoke update,delete,truncate on outreach.audit_events from clinic_outreach_runtime;
revoke delete,truncate on outreach.source_import_rows from clinic_outreach_runtime;
revoke delete,truncate on outreach.source_import_batches from clinic_outreach_runtime;
revoke delete,truncate on outreach.sales_cases from clinic_outreach_runtime;
revoke delete,truncate on outreach.gmail_messages from clinic_outreach_runtime;
revoke delete,truncate on outreach.calendar_appointments from clinic_outreach_runtime;
revoke delete,truncate on outreach.sync_jobs from clinic_outreach_runtime;
revoke delete,truncate on outreach.sync_errors from clinic_outreach_runtime;
revoke delete,truncate on outreach.email_delivery_records from clinic_outreach_runtime;

commit;
