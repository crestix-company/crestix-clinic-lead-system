-- Email Human Review: append-only identity labels. Prepared offline; DO NOT apply in Phase E1.
--
-- Grants target clinic_email_runtime, a role dedicated to Email Research (see
-- email_runtime_role_schema.sql / email_research_schema.sql) -- never the general-purpose
-- Clinic/HP role, so Human Review write access never inherits Clinic/HP/Comdesk write
-- privileges.

begin;

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'clinic_email_runtime') then
    create role clinic_email_runtime nologin;
  end if;
end $$;

create schema if not exists provenance;
grant usage on schema provenance to clinic_email_runtime;

create table if not exists provenance.email_human_reviews (
  id bigint generated always as identity primary key,
  clinic_id bigint not null,
  candidate_id bigint not null,
  email text not null,
  human_decision text not null
    check (human_decision in ('TRUE_POSITIVE','FALSE_POSITIVE','UNCERTAIN')),
  reviewer text not null,
  review_note text not null default '',
  auto_role text not null,
  auto_ownership text not null,
  auto_verification_status text not null,
  auto_identity_decision text not null,
  auto_confidence text not null,
  auto_final_sales_eligible boolean not null,
  auto_primary_email_candidate_id bigint,
  auto_snapshot jsonb not null,
  rule_version text not null,
  reviewed_at timestamptz not null default now(),
  constraint email_human_reviews_candidate_fkey
    foreign key(clinic_id,candidate_id)
    references email_research.email_candidates(clinic_id,candidate_id)
);

create index if not exists email_human_reviews_candidate_latest_idx
  on provenance.email_human_reviews(candidate_id,reviewed_at desc,id desc);
create index if not exists email_human_reviews_clinic_latest_idx
  on provenance.email_human_reviews(clinic_id,reviewed_at desc,id desc);
create index if not exists email_human_reviews_decision_idx
  on provenance.email_human_reviews(human_decision,reviewed_at desc);

alter table provenance.email_human_reviews enable row level security;
grant select,insert on provenance.email_human_reviews to clinic_email_runtime;
grant usage,select on sequence provenance.email_human_reviews_id_seq to clinic_email_runtime;
revoke all on provenance.email_human_reviews from public,anon,authenticated;
revoke update,delete,truncate,references,trigger on provenance.email_human_reviews
  from clinic_email_runtime,public,anon,authenticated;
revoke all on sequence provenance.email_human_reviews_id_seq
  from public,anon,authenticated;

do $$
begin
  if not exists (
    select 1 from pg_policies where schemaname='provenance'
      and tablename='email_human_reviews'
      and policyname='clinic_email_runtime_email_human_reviews_select'
  ) then
    create policy clinic_email_runtime_email_human_reviews_select
      on provenance.email_human_reviews for select to clinic_email_runtime using (true);
  end if;
  if not exists (
    select 1 from pg_policies where schemaname='provenance'
      and tablename='email_human_reviews'
      and policyname='clinic_email_runtime_email_human_reviews_insert'
  ) then
    create policy clinic_email_runtime_email_human_reviews_insert
      on provenance.email_human_reviews for insert to clinic_email_runtime with check (true);
  end if;
end $$;

commit;
