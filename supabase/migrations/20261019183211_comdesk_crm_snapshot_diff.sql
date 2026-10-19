-- Per-clinic Comdesk CRM exports are snapshots, not call-event records.
-- IDs and UUIDs are references only; this migration never creates or changes them.

create unique index if not exists sales_entity_clinics_clinic_common_uidx
  on public.sales_entity_clinics(clinic_id, common_clinic_id);
create unique index if not exists sales_entity_comdesk_identity_common_uuid_uidx
  on public.sales_entity_comdesk_identity(common_clinic_id, comdesk_uuid);

create table if not exists public.sales_entity_comdesk_import_batches (
  source_batch_key text primary key,
  sales_file_sha256 text not null check (sales_file_sha256 ~ '^[0-9a-f]{64}$'),
  clinic_file_sha256 text not null check (clinic_file_sha256 ~ '^[0-9a-f]{64}$'),
  source_snapshot_at timestamptz not null,
  created_at timestamptz not null default now()
);

create table if not exists public.sales_entity_comdesk_crm_snapshots (
  common_clinic_id bigint primary key,
  clinic_id bigint not null,
  comdesk_uuid text not null,
  progress text,
  last_call_at text,
  last_call_by text,
  call_count text,
  project_keys text[] not null default '{}',
  history_text text not null default '',
  history_sha256 text not null check (history_sha256 ~ '^[0-9a-f]{64}$'),
  source_snapshot_at timestamptz not null,
  source_batch_key text not null references public.sales_entity_comdesk_import_batches(source_batch_key) on delete restrict,
  updated_at timestamptz not null default now(),
  constraint sales_entity_comdesk_snapshot_clinic_fk
    foreign key (clinic_id, common_clinic_id)
    references public.sales_entity_clinics(clinic_id, common_clinic_id) on delete restrict,
  constraint sales_entity_comdesk_snapshot_uuid_fk
    foreign key (common_clinic_id, comdesk_uuid)
    references public.sales_entity_comdesk_identity(common_clinic_id, comdesk_uuid) on delete restrict
);
create index if not exists sales_entity_comdesk_snapshot_batch_idx
  on public.sales_entity_comdesk_crm_snapshots(source_batch_key);
create index if not exists sales_entity_comdesk_snapshot_clinic_idx
  on public.sales_entity_comdesk_crm_snapshots(clinic_id);

-- A row means the free-text snapshot changed. It is explicitly not a call event.
create table if not exists public.sales_entity_comdesk_history_changes (
  id bigint generated always as identity primary key,
  common_clinic_id bigint not null,
  clinic_id bigint not null,
  comdesk_uuid text not null,
  source_batch_key text not null references public.sales_entity_comdesk_import_batches(source_batch_key) on delete restrict,
  source_snapshot_at timestamptz not null,
  previous_history text not null,
  previous_history_sha256 text not null check (previous_history_sha256 ~ '^[0-9a-f]{64}$'),
  current_history text not null,
  current_history_sha256 text not null check (current_history_sha256 ~ '^[0-9a-f]{64}$'),
  created_at timestamptz not null default now(),
  unique (common_clinic_id, source_batch_key),
  foreign key (clinic_id, common_clinic_id)
    references public.sales_entity_clinics(clinic_id, common_clinic_id) on delete restrict,
  foreign key (common_clinic_id, comdesk_uuid)
    references public.sales_entity_comdesk_identity(common_clinic_id, comdesk_uuid) on delete restrict
);
create index if not exists sales_entity_comdesk_history_changes_entity_idx
  on public.sales_entity_comdesk_history_changes(common_clinic_id, source_snapshot_at);
create index if not exists sales_entity_comdesk_history_changes_batch_idx
  on public.sales_entity_comdesk_history_changes(source_batch_key);

create table if not exists public.sales_entity_comdesk_import_holds (
  source_batch_key text not null references public.sales_entity_comdesk_import_batches(source_batch_key) on delete restrict,
  source_uuid text not null default '',
  reason text not null,
  row_fingerprint text not null,
  details jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  primary key (source_batch_key, row_fingerprint)
);
create index if not exists sales_entity_comdesk_import_holds_batch_idx
  on public.sales_entity_comdesk_import_holds(source_batch_key);

alter table public.sales_entity_comdesk_import_batches enable row level security;
alter table public.sales_entity_comdesk_crm_snapshots enable row level security;
alter table public.sales_entity_comdesk_history_changes enable row level security;
alter table public.sales_entity_comdesk_import_holds enable row level security;

revoke all on public.sales_entity_comdesk_import_batches,
  public.sales_entity_comdesk_crm_snapshots,
  public.sales_entity_comdesk_history_changes,
  public.sales_entity_comdesk_import_holds from public, anon, authenticated, service_role;
grant select, insert on public.sales_entity_comdesk_import_batches to service_role;
grant select, insert on public.sales_entity_comdesk_crm_snapshots to service_role;
grant update (progress, last_call_at, last_call_by, call_count, project_keys,
  history_text, history_sha256, source_snapshot_at, source_batch_key, updated_at)
  on public.sales_entity_comdesk_crm_snapshots to service_role;
grant select, insert on public.sales_entity_comdesk_history_changes,
  public.sales_entity_comdesk_import_holds to service_role;
revoke all on sequence public.sales_entity_comdesk_history_changes_id_seq
  from public, anon, authenticated, service_role;
grant usage, select on sequence public.sales_entity_comdesk_history_changes_id_seq to service_role;
