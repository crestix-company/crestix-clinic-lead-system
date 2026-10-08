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

alter table provenance.hp_human_reviews enable row level security;

grant select, insert on provenance.hp_human_reviews to clinic_runtime;
revoke all on provenance.hp_human_reviews from anon, authenticated, public;

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
end $$;

commit;

-- Read-only validation after apply:
--
-- select to_regclass('provenance.hp_human_reviews');
-- select relrowsecurity
-- from pg_class c join pg_namespace n on n.oid=c.relnamespace
-- where n.nspname='provenance' and c.relname='hp_human_reviews';
--
-- select grantee,privilege_type
-- from information_schema.table_privileges
-- where table_schema='provenance' and table_name='hp_human_reviews'
-- order by grantee,privilege_type;
--
-- expect:
--   clinic_runtime: INSERT, SELECT
--   anon/authenticated/PUBLIC: no rows
