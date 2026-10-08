-- HP Human Review: append-only human labels for HP REVIEW decisions.
--
-- Prepared offline. Do NOT apply while a Production HP worker is being cut over.
-- Safe-by-construction:
--   * additive table only
--   * no UPDATE/DELETE of existing Production rows
--   * no grants to anon/authenticated/PUBLIC
--   * clinic_runtime gets SELECT + INSERT only
--   * RLS enabled with role-scoped policies

begin;

create table if not exists provenance.hp_human_reviews (
  id text primary key,
  clinic_id bigint not null references public.clinics(id),
  hp_checked_at text not null,
  research_job_id text not null default '',
  auto_run_id text not null default '',
  selected_url text not null default '',
  human_decision text not null
    check (human_decision in (
      'OFFICIAL',
      'ORGANIZATION_PAGE',
      'NOT_OFFICIAL',
      'ACCESS_RESTRICTED',
      'UNCERTAIN'
    )),
  reviewer text not null,
  review_note text not null default '',
  rule_version text not null,
  auto_snapshot jsonb not null,
  reviewed_at timestamptz not null default now()
);

create index if not exists hp_human_reviews_attempt_idx
  on provenance.hp_human_reviews(clinic_id,hp_checked_at,reviewed_at desc);

create index if not exists hp_human_reviews_decision_idx
  on provenance.hp_human_reviews(human_decision,reviewed_at desc);

create table if not exists provenance.hp_human_review_research_runs (
  id text primary key,
  human_review_id text not null references provenance.hp_human_reviews(id),
  clinic_id bigint not null references public.clinics(id),
  selected_url text not null,
  status text not null check (status in ('DONE','FAILED')),
  identity_source text not null default 'HUMAN_REVIEW'
    check (identity_source='HUMAN_REVIEW'),
  treatment_categories jsonb not null default '[]'::jsonb,
  treatment_count integer not null default 0,
  error_detail text not null default '',
  started_at timestamptz not null default now(),
  finished_at timestamptz not null default now()
);

create index if not exists hp_human_review_research_runs_review_idx
  on provenance.hp_human_review_research_runs(human_review_id,finished_at desc);

create index if not exists hp_human_review_research_runs_clinic_idx
  on provenance.hp_human_review_research_runs(clinic_id,finished_at desc);

alter table provenance.hp_human_reviews enable row level security;
alter table provenance.hp_human_review_research_runs enable row level security;

grant select, insert on provenance.hp_human_reviews to clinic_runtime;
grant select, insert on provenance.hp_human_review_research_runs to clinic_runtime;
revoke all on provenance.hp_human_reviews from anon, authenticated, public;
revoke all on provenance.hp_human_review_research_runs from anon, authenticated, public;

do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname='provenance'
      and tablename='hp_human_reviews'
      and policyname='clinic_runtime_hp_human_review_select'
  ) then
    create policy clinic_runtime_hp_human_review_select
      on provenance.hp_human_reviews
      for select
      to clinic_runtime
      using (true);
  end if;

  if not exists (
    select 1 from pg_policies
    where schemaname='provenance'
      and tablename='hp_human_reviews'
      and policyname='clinic_runtime_hp_human_review_insert'
  ) then
    create policy clinic_runtime_hp_human_review_insert
      on provenance.hp_human_reviews
      for insert
      to clinic_runtime
      with check (true);
  end if;


  if not exists (
    select 1 from pg_policies
    where schemaname='provenance'
      and tablename='hp_human_review_research_runs'
      and policyname='clinic_runtime_hp_human_review_run_select'
  ) then
    create policy clinic_runtime_hp_human_review_run_select
      on provenance.hp_human_review_research_runs
      for select
      to clinic_runtime
      using (true);
  end if;

  if not exists (
    select 1 from pg_policies
    where schemaname='provenance'
      and tablename='hp_human_review_research_runs'
      and policyname='clinic_runtime_hp_human_review_run_insert'
  ) then
    create policy clinic_runtime_hp_human_review_run_insert
      on provenance.hp_human_review_research_runs
      for insert
      to clinic_runtime
      with check (true);
  end if;
end $$;

commit;

-- Read-only validation after apply:
--
-- select to_regclass('provenance.hp_human_reviews');
-- select to_regclass('provenance.hp_human_review_research_runs');
-- select relrowsecurity
-- from pg_class c join pg_namespace n on n.oid=c.relnamespace
-- where n.nspname='provenance' and c.relname='hp_human_reviews';
--
-- select grantee,privilege_type
-- from information_schema.table_privileges
-- where table_schema='provenance'
--   and table_name in ('hp_human_reviews','hp_human_review_research_runs')
-- order by grantee,privilege_type;
--
-- expect:
--   clinic_runtime: INSERT, SELECT
--   anon/authenticated/PUBLIC: no rows
