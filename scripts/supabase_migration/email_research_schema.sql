-- Email Research formal SSOT. Prepared offline; DO NOT apply during Phase E1.
-- Additive only. Legacy public.clinic_email_enrichment is intentionally untouched.
--
-- Supports two deployment topologies from one script, auto-detected from the database
-- this migration is applied to (see email_target_reader.py / EMAIL_RESEARCH_DB_MODE for
-- the matching application-level fail-closed guard):
--   cross_database sidecar (default, legacy) -- public.clinics does not exist in this
--     database. email_research.clinic_targets stays a physically-synced snapshot table,
--     written by sync_targets() from a separate read-only Clinic/HP connection.
--   same_database integration -- public.clinics exists in this same database.
--     email_research.clinic_targets becomes a live view over public.clinics /
--     hp_research.clinic_hp_research instead of a duplicated snapshot, so claim_next()
--     always reads current eligibility. manual_exclude moves onto
--     email_research.clinic_email_status, which stays the sole place claim/lease/
--     checkpoint/manual-exclude state lives in either topology.

begin;

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'clinic_email_runtime') then
    create role clinic_email_runtime nologin;
  end if;
end $$;

create schema if not exists email_research;

-- cross_database sidecar only: a plain, physically-synced snapshot table. Skipped
-- entirely in same_database mode, where clinic_targets is created as a view further
-- below instead (see the "same_database integration" block after clinic_email_status).
do $$
begin
  if to_regclass('public.clinics') is null then
    if to_regclass('email_research.clinic_targets') is null then
      create table email_research.clinic_targets (
        clinic_id bigint primary key,
        clinic_uuid text not null unique,
        clinic_name text not null default '',
        prefecture text not null default '',
        city text not null default '',
        address text not null default '',
        phone text not null default '',
        official_hp_url text not null,
        hp_verified_at timestamptz,
        manual_exclude boolean not null default false,
        eligible boolean not null default true,
        synced_at timestamptz not null default now(),
        constraint clinic_targets_verified_url_check check (btrim(official_hp_url)<>'')
      );
    end if;
    create index if not exists clinic_targets_eligible_idx
      on email_research.clinic_targets(eligible,clinic_id);
  end if;
end $$;

create table if not exists email_research.email_candidates (
  candidate_id bigint generated always as identity primary key,
  clinic_id bigint not null,
  research_run_id text not null default '',
  email text not null,
  normalized_email text not null,
  discovery_source text not null
    check (discovery_source in ('OFFICIAL_HP','NAVII','SEARCH_QUERY','AI_OVERVIEW')),
  source_url text not null default '',
  search_query text not null default '',
  role text not null
    check (role in ('GENERAL','OFFICE','RESERVATION','RECRUIT','RECRUITING',
                    'MEDICAL_COOPERATION','PERSONAL','UNKNOWN')),
  ownership text not null
    check (ownership in ('CLINIC_OWNED','SAME_MEDICAL_CORPORATION',
                         'THIRD_PARTY_OWNED','OWNERSHIP_UNKNOWN')),
  -- `ownership` remains a compatibility alias for `email_ownership`.
  email_ownership text not null
    check (email_ownership in ('CLINIC_OWNED','SAME_MEDICAL_CORPORATION',
                               'THIRD_PARTY_OWNED','OWNERSHIP_UNKNOWN')),
  source_owner_type text not null default 'UNKNOWN'
    check (source_owner_type in ('OFFICIAL_CLINIC','TRUSTED_PUBLIC_DIRECTORY',
                                 'THIRD_PARTY_PORTAL','GOVERNMENT','ASSOCIATION','VENDOR','UNKNOWN')),
  verification_status text not null,
  auto_decision text not null check (auto_decision in ('AUTO_TRUE','AUTO_FALSE','REVIEW')),
  auto_identity_decision text not null
    check (auto_identity_decision in ('TRUE','REVIEW','FALSE')),
  confidence text not null check (confidence in ('HIGH','MEDIUM','LOW')),
  clinic_name_match boolean,
  prefecture_match boolean,
  city_match boolean,
  verification_reason text not null default '',
  rejection_reason text not null default '',
  sales_usable text not null check (sales_usable in ('YES','NO','REVIEW')),
  hard_block_codes text[] not null default '{}'::text[],
  final_sales_eligible boolean not null default false,
  -- Legacy identity relation only. Primary confirmation never writes back to
  -- public.clinic_email_enrichment; this column is for read-side lookups (e.g. surfacing
  -- an already-enriched legacy row next to a new Candidate) before that cutover decision.
  existing_email_enrichment_id bigint,
  auto_snapshot jsonb not null default '{}'::jsonb,
  rule_version text not null,
  researched_at timestamptz not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint email_candidates_clinic_normalized_email_key unique(clinic_id,normalized_email),
  constraint email_candidates_clinic_candidate_key unique(clinic_id,candidate_id),
  constraint email_candidates_final_eligible_check check (
    not final_sales_eligible or (
      role in ('GENERAL','OFFICE')
      and email_ownership in ('CLINIC_OWNED','SAME_MEDICAL_CORPORATION')
      and ownership=email_ownership
      and clinic_name_match is true
      and prefecture_match is true
      and city_match is true
      and cardinality(hard_block_codes)=0
      and sales_usable='YES'
    )
  )
);

create index if not exists email_candidates_clinic_idx
  on email_research.email_candidates(clinic_id);
create index if not exists email_candidates_decision_confidence_idx
  on email_research.email_candidates(auto_decision,confidence,researched_at desc);
create index if not exists email_candidates_review_queue_idx
  on email_research.email_candidates(researched_at,candidate_id)
  where auto_decision='REVIEW';
create index if not exists email_candidates_source_idx
  on email_research.email_candidates(discovery_source,researched_at desc);

create table if not exists email_research.email_rejection_evidence (
  id bigint generated always as identity primary key,
  clinic_id bigint not null,
  research_run_id text not null default '',
  normalized_email text not null,
  source_url text not null default '',
  source_type text not null default '',
  auto_decision text not null default 'AUTO_FALSE' check (auto_decision='AUTO_FALSE'),
  rejection_reason text not null,
  hard_block_codes text[] not null default '{}'::text[],
  rejected_at timestamptz not null default now(),
  auto_snapshot jsonb not null default '{}'::jsonb
);
create index if not exists email_rejection_evidence_clinic_idx
  on email_research.email_rejection_evidence(clinic_id,rejected_at desc);
create index if not exists email_rejection_evidence_email_idx
  on email_research.email_rejection_evidence(normalized_email,rejected_at desc);

create table if not exists email_research.clinic_email_status (
  clinic_id bigint primary key,
  research_status text not null default 'NOT_RESEARCHED'
    check (research_status in ('NOT_RESEARCHED','RUNNING','FOUND','NO_EMAIL_FOUND','REVIEW','ERROR')),
  current_run_id text not null default '',
  candidate_count integer not null default 0 check (candidate_count>=0),
  primary_email_candidate_id bigint,
  rule_version text not null default '',
  last_error text not null default '',
  started_at timestamptz,
  researched_at timestamptz,
  updated_at timestamptz not null default now(),
  claim_token text not null default '',
  claimed_by text not null default '',
  claim_expires_at timestamptz,
  attempts integer not null default 0 check (attempts>=0),
  metrics jsonb not null default '{}'::jsonb,
  -- Lives here (not on clinic_targets) in both topologies: manual exclude, like claim/
  -- lease/checkpoint state, is Email-side-owned state that must survive clinic_targets
  -- becoming a live view in same_database mode.
  manual_exclude boolean not null default false,
  constraint clinic_email_status_primary_candidate_fkey
    foreign key(clinic_id,primary_email_candidate_id)
    references email_research.email_candidates(clinic_id,candidate_id)
);

create index if not exists clinic_email_status_research_queue_idx
  on email_research.clinic_email_status(research_status,updated_at,clinic_id)
  where primary_email_candidate_id is null;
create index if not exists clinic_email_status_primary_candidate_idx
  on email_research.clinic_email_status(primary_email_candidate_id)
  where primary_email_candidate_id is not null;
create index if not exists clinic_email_status_claim_expiry_idx
  on email_research.clinic_email_status(claim_expires_at)
  where research_status='RUNNING';

-- same_database integration only, from here to the end of this do block.
do $$
begin
  if to_regclass('public.clinics') is not null then
    -- Candidate identity is now directly checkable against the Clinic SoT.
    if not exists (
      select 1 from pg_constraint where conname = 'email_candidates_clinic_fkey'
    ) then
      alter table email_research.email_candidates
        add constraint email_candidates_clinic_fkey
        foreign key(clinic_id) references public.clinics(id);
    end if;

    if to_regclass('public.clinic_email_enrichment') is not null
        and not exists (
          select 1 from pg_constraint where conname = 'email_candidates_legacy_enrichment_fkey'
        ) then
      alter table email_research.email_candidates
        add constraint email_candidates_legacy_enrichment_fkey
        foreign key(existing_email_enrichment_id) references public.clinic_email_enrichment(id)
        on delete set null;
    end if;

    -- clinic_targets stops being a duplicated snapshot and becomes a live read of
    -- public.clinics / hp_research.clinic_hp_research, joined with the manual_exclude
    -- flag that now lives on clinic_email_status. Carry forward any manual_exclude=true
    -- rows from a pre-existing physical snapshot (e.g. a prior cross_database deployment
    -- migrating into same_database mode) before dropping the table.
    if exists (
      select 1 from pg_class where relname = 'clinic_targets' and relkind = 'r'
        and relnamespace = 'email_research'::regnamespace
    ) then
      update email_research.clinic_email_status s
        set manual_exclude = true
        from email_research.clinic_targets t
        where t.clinic_id = s.clinic_id and t.manual_exclude = true;
      drop table email_research.clinic_targets;
    end if;

    -- NOTE: claim_next() in same_database mode must select "FOR UPDATE OF s" only (not
    -- "OF s,c") -- Postgres refuses FOR UPDATE on a non-simple multi-table view. This is
    -- enforced in SupabaseEmailResearchRepository, selected via same_database=True.
    execute $view$
      create or replace view email_research.clinic_targets as
      select
        c.id as clinic_id,
        c.uuid as clinic_uuid,
        coalesce(c.clinic_name,'') as clinic_name,
        coalesce(c.prefecture,'') as prefecture,
        ''::text as city,
        coalesce(c.address,'') as address,
        coalesce(c.phone,'') as phone,
        coalesce(nullif(btrim(c.hp_url),''),
          case when c.maps_presence_status='MAPS_MATCHED_WEBSITE'
            then nullif(btrim(c.maps_website_url),'') end) as official_hp_url,
        r.researched_at as hp_verified_at,
        coalesce(s.manual_exclude,false) as manual_exclude,
        (
          c.active=true and c.merged_into is null and c.merge_hold=false
          and coalesce(btrim(c.exclude_reason),'')=''
          and c.hp_status='VERIFIED'
          and (coalesce(btrim(c.hp_url),'')<>'' or
               (c.maps_presence_status='MAPS_MATCHED_WEBSITE'
                and coalesce(btrim(c.maps_website_url),'')<>''))
        ) as eligible,
        now() as synced_at
      from public.clinics c
      left join hp_research.clinic_hp_research r on r.clinic_id=c.id
      left join email_research.clinic_email_status s on s.clinic_id=c.id
    $view$;
  end if;
end $$;

-- Primary eligibility DB guard. Applies in both topologies: this is a property of the
-- Primary SoT itself, independent of where Clinic/HP state lives. The composite FK on
-- clinic_email_status.primary_email_candidate_id already makes cross-clinic assignment
-- impossible and already caps a clinic at one Primary (clinic_id is clinic_email_status's
-- primary key); this trigger adds the remaining DB-level condition the Repository also
-- enforces in Python (set_primary_candidate): final_sales_eligible=true and no
-- hard_block_codes. A plain BEFORE trigger (not a constraint trigger) is enough because
-- the check only ever needs the about-to-be-committed row plus a single lookup, with no
-- cross-row ordering concern that would require deferring to commit time.
create or replace function email_research.enforce_primary_email_candidate()
returns trigger
language plpgsql
security definer
set search_path = email_research, pg_temp
as $fn$
declare
  v_clinic_id bigint;
  v_eligible boolean;
  v_hard_block_count int;
begin
  if new.primary_email_candidate_id is null then
    return new;
  end if;
  if tg_op = 'UPDATE' and new.primary_email_candidate_id is not distinct from old.primary_email_candidate_id then
    return new;
  end if;
  select clinic_id,final_sales_eligible,cardinality(hard_block_codes)
    into v_clinic_id,v_eligible,v_hard_block_count
    from email_research.email_candidates
    where candidate_id = new.primary_email_candidate_id
    for share;
  if v_clinic_id is null then
    raise exception 'primary_email_candidate_id % does not exist', new.primary_email_candidate_id;
  end if;
  if v_clinic_id <> new.clinic_id then
    raise exception 'candidate % belongs to clinic %, not clinic %',
      new.primary_email_candidate_id, v_clinic_id, new.clinic_id;
  end if;
  if v_eligible is not true then
    raise exception 'candidate % is not final_sales_eligible', new.primary_email_candidate_id;
  end if;
  if v_hard_block_count > 0 then
    raise exception 'candidate % has hard_block_codes set', new.primary_email_candidate_id;
  end if;
  return new;
end;
$fn$;

drop trigger if exists clinic_email_status_primary_guard on email_research.clinic_email_status;
create trigger clinic_email_status_primary_guard
  before insert or update of primary_email_candidate_id on email_research.clinic_email_status
  for each row execute function email_research.enforce_primary_email_candidate();

create table if not exists email_research.email_auto_runs (
  run_id text primary key,
  status text not null
    check (status in ('RUNNING','PAUSED','STOPPING','STOPPED','COMPLETED','ERROR')),
  worker_count integer not null default 12 check (worker_count between 1 and 64),
  search_query_limit integer not null default 2 check (search_query_limit between 0 and 2),
  read_timeout_seconds numeric not null default 20 check (read_timeout_seconds>0),
  retry_count integer not null default 1 check (retry_count between 0 and 5),
  rule_version text not null,
  started_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  finished_at timestamptz,
  stop_requested boolean not null default false,
  metrics jsonb not null default '{}'::jsonb,
  last_error text not null default ''
);

alter table email_research.email_candidates enable row level security;
alter table email_research.clinic_email_status enable row level security;
alter table email_research.email_auto_runs enable row level security;
alter table email_research.email_rejection_evidence enable row level security;

-- clinic_targets only gets RLS/policies when it is still a physical table
-- (cross_database sidecar). In same_database mode it is a view over public.clinics /
-- hp_research.clinic_hp_research / clinic_email_status -- RLS and CREATE POLICY cannot
-- target a view at all, and read access is already scoped by the plain SELECT grant plus
-- whatever policies those base tables already carry.
do $$
begin
  if exists (
    select 1 from pg_class where relname='clinic_targets' and relkind='r'
      and relnamespace='email_research'::regnamespace
  ) then
    execute 'alter table email_research.clinic_targets enable row level security';
  end if;
end $$;

revoke all on schema email_research from public,anon,authenticated;
grant usage on schema email_research to clinic_email_runtime;
revoke all on all tables in schema email_research from public,anon,authenticated;
revoke all on all sequences in schema email_research from public,anon,authenticated;
grant select on email_research.clinic_targets to clinic_email_runtime;
do $$
begin
  if exists (
    select 1 from pg_class where relname='clinic_targets' and relkind='r'
      and relnamespace='email_research'::regnamespace
  ) then
    execute 'grant insert,update on email_research.clinic_targets to clinic_email_runtime';
  end if;
end $$;
grant select,insert,update on email_research.email_candidates to clinic_email_runtime;
grant select,insert,update on email_research.clinic_email_status to clinic_email_runtime;
grant select,insert,update on email_research.email_auto_runs to clinic_email_runtime;
grant select,insert on email_research.email_rejection_evidence to clinic_email_runtime;
grant usage,select on sequence email_research.email_candidates_candidate_id_seq to clinic_email_runtime;
grant usage,select on sequence email_research.email_rejection_evidence_id_seq to clinic_email_runtime;

do $$
declare
  v_table_name text;
  v_operation text;
  v_policy_name text;
begin
  foreach v_table_name in array array['clinic_targets','email_candidates','clinic_email_status','email_auto_runs'] loop
    if v_table_name = 'clinic_targets' and not exists (
      select 1 from pg_class where relname='clinic_targets' and relkind='r'
        and relnamespace='email_research'::regnamespace
    ) then
      continue;
    end if;
    foreach v_operation in array array['select','insert','update'] loop
      v_policy_name := 'clinic_email_runtime_' || v_table_name || '_' || v_operation;
      if not exists (
        select 1 from pg_policies
        where schemaname='email_research'
          and tablename=v_table_name and policyname=v_policy_name
      ) then
        if v_operation='select' then
          execute format('create policy %I on email_research.%I for select to clinic_email_runtime using (true)',v_policy_name,v_table_name);
        elsif v_operation='insert' then
          execute format('create policy %I on email_research.%I for insert to clinic_email_runtime with check (true)',v_policy_name,v_table_name);
        else
          execute format('create policy %I on email_research.%I for update to clinic_email_runtime using (true) with check (true)',v_policy_name,v_table_name);
        end if;
      end if;
    end loop;
  end loop;
end $$;

do $$ begin
  if not exists (select 1 from pg_policies where schemaname='email_research'
      and tablename='email_rejection_evidence' and policyname='clinic_email_runtime_email_rejection_select') then
    create policy clinic_email_runtime_email_rejection_select on email_research.email_rejection_evidence
      for select to clinic_email_runtime using (true);
  end if;
  if not exists (select 1 from pg_policies where schemaname='email_research'
      and tablename='email_rejection_evidence' and policyname='clinic_email_runtime_email_rejection_insert') then
    create policy clinic_email_runtime_email_rejection_insert on email_research.email_rejection_evidence
      for insert to clinic_email_runtime with check (true);
  end if;
end $$;

commit;

-- Read-only verification after a separately approved apply:
-- select to_regclass('email_research.email_candidates');
-- select to_regclass('email_research.clinic_email_status');
-- select to_regclass('email_research.email_auto_runs');
-- select * from pg_policies where schemaname='email_research' order by tablename,policyname;
