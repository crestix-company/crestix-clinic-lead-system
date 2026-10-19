-- Per-clinic Comdesk CRM exports are snapshots, not call-event records.
-- IDs and UUIDs are references only; this migration never creates or changes them.

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'clinic_runtime') then
    raise exception 'clinic_runtime role must exist before applying Comdesk snapshot migration';
  end if;
  if not exists (select 1 from pg_roles where rolname = 'clinic_comdesk_snapshot_runtime') then
    create role clinic_comdesk_snapshot_runtime nologin nosuperuser nocreatedb nocreaterole
      noreplication nobypassrls;
  end if;
end $$;

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
  filter_project_name text,
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
alter table public.sales_entity_comdesk_crm_snapshots
  add column if not exists filter_project_name text;
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
alter table public.sales_entity_comdesk_import_batches force row level security;
alter table public.sales_entity_comdesk_crm_snapshots force row level security;
alter table public.sales_entity_comdesk_history_changes force row level security;
alter table public.sales_entity_comdesk_import_holds force row level security;

revoke all on public.sales_entity_comdesk_import_batches,
  public.sales_entity_comdesk_crm_snapshots,
  public.sales_entity_comdesk_history_changes,
  public.sales_entity_comdesk_import_holds from public, anon, authenticated,
  service_role, clinic_runtime, clinic_comdesk_snapshot_runtime;
revoke all on public.sales_entity_comdesk_identity, public.sales_entity_identity_audit,
  public.sales_entity_clinics from clinic_comdesk_snapshot_runtime;
revoke all on public.clinics from clinic_comdesk_snapshot_runtime;
grant select (id, clinic_name, address, phone) on public.clinics to clinic_comdesk_snapshot_runtime;
grant select on public.sales_entity_clinics, public.sales_entity_comdesk_identity
  to clinic_comdesk_snapshot_runtime;
grant insert (common_clinic_id, comdesk_uuid, source, evidence, verified_at)
  on public.sales_entity_comdesk_identity to clinic_comdesk_snapshot_runtime;
grant insert (event_key, common_clinic_id, action, before_state, after_state, reason, actor)
  on public.sales_entity_identity_audit to clinic_comdesk_snapshot_runtime;
grant usage, select on sequence public.sales_entity_identity_audit_id_seq
  to clinic_comdesk_snapshot_runtime;
grant execute on function public.prevent_sales_entity_uuid_replacement()
  to clinic_comdesk_snapshot_runtime;
grant select, insert on public.sales_entity_comdesk_import_batches to clinic_comdesk_snapshot_runtime;
grant select, insert on public.sales_entity_comdesk_crm_snapshots to clinic_comdesk_snapshot_runtime;
grant update (progress, last_call_at, last_call_by, call_count, project_keys,
  filter_project_name, history_text, history_sha256, source_snapshot_at, source_batch_key, updated_at)
  on public.sales_entity_comdesk_crm_snapshots to clinic_comdesk_snapshot_runtime;
grant select, insert on public.sales_entity_comdesk_history_changes,
  public.sales_entity_comdesk_import_holds to clinic_comdesk_snapshot_runtime;
revoke all on sequence public.sales_entity_comdesk_history_changes_id_seq
  from public, anon, authenticated, service_role, clinic_runtime, clinic_comdesk_snapshot_runtime;
revoke all on sequence public.sales_entity_identity_audit_id_seq
  from public, anon, authenticated, service_role, clinic_runtime, clinic_comdesk_snapshot_runtime;
grant usage, select on sequence public.sales_entity_comdesk_history_changes_id_seq to clinic_comdesk_snapshot_runtime;
grant usage, select on sequence public.sales_entity_identity_audit_id_seq to clinic_comdesk_snapshot_runtime;

drop policy if exists clinic_comdesk_snapshot_runtime_batches on public.sales_entity_comdesk_import_batches;
create policy clinic_comdesk_snapshot_runtime_batches on public.sales_entity_comdesk_import_batches
  for all to clinic_comdesk_snapshot_runtime using (true) with check (true);
drop policy if exists clinic_comdesk_snapshot_runtime_snapshots on public.sales_entity_comdesk_crm_snapshots;
create policy clinic_comdesk_snapshot_runtime_snapshots on public.sales_entity_comdesk_crm_snapshots
  for all to clinic_comdesk_snapshot_runtime using (true) with check (true);
drop policy if exists clinic_comdesk_snapshot_runtime_history on public.sales_entity_comdesk_history_changes;
create policy clinic_comdesk_snapshot_runtime_history on public.sales_entity_comdesk_history_changes
  for all to clinic_comdesk_snapshot_runtime using (true) with check (true);
drop policy if exists clinic_comdesk_snapshot_runtime_holds on public.sales_entity_comdesk_import_holds;
create policy clinic_comdesk_snapshot_runtime_holds on public.sales_entity_comdesk_import_holds
  for all to clinic_comdesk_snapshot_runtime using (true) with check (true);
drop policy if exists clinic_comdesk_snapshot_runtime_identity_select on public.sales_entity_comdesk_identity;
create policy clinic_comdesk_snapshot_runtime_identity_select on public.sales_entity_comdesk_identity
  for select to clinic_comdesk_snapshot_runtime using (true);
drop policy if exists clinic_comdesk_snapshot_runtime_identity_insert on public.sales_entity_comdesk_identity;
create policy clinic_comdesk_snapshot_runtime_identity_insert on public.sales_entity_comdesk_identity
  for insert to clinic_comdesk_snapshot_runtime with check (true);
drop policy if exists clinic_comdesk_snapshot_runtime_identity_audit_insert on public.sales_entity_identity_audit;
create policy clinic_comdesk_snapshot_runtime_identity_audit_insert on public.sales_entity_identity_audit
  for insert to clinic_comdesk_snapshot_runtime with check (true);
drop policy if exists clinic_comdesk_snapshot_runtime_clinics_select on public.sales_entity_clinics;
create policy clinic_comdesk_snapshot_runtime_clinics_select on public.sales_entity_clinics
  for select to clinic_comdesk_snapshot_runtime using (true);
drop policy if exists clinic_comdesk_snapshot_runtime_public_clinics_select on public.clinics;
create policy clinic_comdesk_snapshot_runtime_public_clinics_select on public.clinics
  for select to clinic_comdesk_snapshot_runtime using (true);

grant clinic_comdesk_snapshot_runtime to clinic_runtime;
revoke all on schema public from clinic_comdesk_snapshot_runtime;
grant usage on schema public to clinic_comdesk_snapshot_runtime;
