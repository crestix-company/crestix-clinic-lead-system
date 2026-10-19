-- Additive common clinic identity layer.
-- public.clinics.id and source records remain immutable and are never deleted.

create table if not exists public.sales_entities (
  common_clinic_id bigint generated always as identity primary key,
  seed_clinic_id bigint unique references public.clinics(id) on delete restrict,
  canonical_name text not null default '',
  canonical_address text not null default '',
  canonical_phone text not null default '',
  status text not null default 'ACTIVE' check (status in ('ACTIVE', 'HOLD', 'RETIRED')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.sales_entity_clinics (
  clinic_id bigint primary key references public.clinics(id) on delete restrict,
  common_clinic_id bigint not null references public.sales_entities(common_clinic_id) on delete restrict,
  match_rule text not null,
  match_evidence jsonb not null default '{}'::jsonb,
  reviewed_by text,
  reviewed_at timestamptz,
  created_at timestamptz not null default now(),
  check ((reviewed_by is null) = (reviewed_at is null))
);
create index if not exists sales_entity_clinics_entity_idx
  on public.sales_entity_clinics(common_clinic_id, clinic_id);

-- One entity has at most one Comdesk UUID; one UUID has at most one entity.
-- No default/generator exists: an unassigned clinic remains UUID-less.
create table if not exists public.sales_entity_comdesk_identity (
  common_clinic_id bigint primary key references public.sales_entities(common_clinic_id) on delete restrict,
  comdesk_uuid text not null unique check (btrim(comdesk_uuid) <> ''),
  source text not null,
  evidence jsonb not null default '{}'::jsonb,
  verified_at timestamptz not null,
  created_at timestamptz not null default now()
);

create or replace function public.prevent_sales_entity_uuid_replacement()
returns trigger language plpgsql
set search_path = pg_catalog
as $$
begin
  if tg_op = 'DELETE' then
    raise exception 'Comdesk UUID identity cannot be deleted; resolve conflicts through an audited review';
  end if;
  if new.comdesk_uuid is distinct from old.comdesk_uuid
     or new.common_clinic_id is distinct from old.common_clinic_id then
    raise exception 'Comdesk UUID identity is immutable; conflicting changes must be held for review';
  end if;
  return new;
end;
$$;
drop trigger if exists sales_entity_comdesk_identity_immutable on public.sales_entity_comdesk_identity;
create trigger sales_entity_comdesk_identity_immutable
  before update of common_clinic_id, comdesk_uuid or delete on public.sales_entity_comdesk_identity
  for each row execute function public.prevent_sales_entity_uuid_replacement();
revoke all on function public.prevent_sales_entity_uuid_replacement() from public, anon, authenticated;
grant execute on function public.prevent_sales_entity_uuid_replacement() to service_role;

create table if not exists public.sales_entity_project_memberships (
  common_clinic_id bigint not null references public.sales_entities(common_clinic_id) on delete restrict,
  project_key text not null check (btrim(project_key) <> ''),
  created_at timestamptz not null default now(),
  primary key (common_clinic_id, project_key)
);

create table if not exists public.sales_entity_resolution_queue (
  id bigint generated always as identity primary key,
  source_name text not null,
  source_hash text not null,
  source_row_number integer not null,
  clinic_ids bigint[] not null default '{}',
  common_clinic_ids bigint[] not null default '{}',
  reason text not null,
  status text not null default 'PENDING' check (status in ('PENDING', 'APPROVED', 'REJECTED')),
  decision_note text not null default '',
  reviewed_by text,
  reviewed_at timestamptz,
  created_at timestamptz not null default now(),
  unique (source_name, source_hash, source_row_number),
  check ((reviewed_by is null) = (reviewed_at is null))
);

create table if not exists public.sales_entity_identity_audit (
  id bigint generated always as identity primary key,
  event_key text not null unique,
  common_clinic_id bigint not null references public.sales_entities(common_clinic_id) on delete restrict,
  action text not null,
  before_state jsonb not null default '{}'::jsonb,
  after_state jsonb not null default '{}'::jsonb,
  reason text not null,
  actor text not null,
  created_at timestamptz not null default now()
);
create index if not exists sales_entity_identity_audit_entity_idx
  on public.sales_entity_identity_audit(common_clinic_id);

alter table public.sales_entities enable row level security;
alter table public.sales_entity_clinics enable row level security;
alter table public.sales_entity_comdesk_identity enable row level security;
alter table public.sales_entity_project_memberships enable row level security;
alter table public.sales_entity_resolution_queue enable row level security;
alter table public.sales_entity_identity_audit enable row level security;

revoke all on public.sales_entities, public.sales_entity_clinics,
  public.sales_entity_comdesk_identity, public.sales_entity_project_memberships,
  public.sales_entity_resolution_queue, public.sales_entity_identity_audit
  from public, anon, authenticated, service_role;
grant select, insert, update on public.sales_entities, public.sales_entity_clinics,
  public.sales_entity_comdesk_identity, public.sales_entity_project_memberships,
  public.sales_entity_resolution_queue, public.sales_entity_identity_audit to service_role;
revoke all on sequence public.sales_entities_common_clinic_id_seq,
  public.sales_entity_resolution_queue_id_seq,
  public.sales_entity_identity_audit_id_seq
  from public, anon, authenticated, service_role;
grant usage, select on sequence public.sales_entities_common_clinic_id_seq,
  public.sales_entity_resolution_queue_id_seq,
  public.sales_entity_identity_audit_id_seq to service_role;
