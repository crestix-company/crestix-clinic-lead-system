-- Dental Sales Tags: additive sidecar for dental sales prioritisation.
--
-- Existing clinic master / HP / Treatment rows are never rewritten by this schema.
-- Auto assignments are replaceable derived data; human labels are append-only training data.
--
-- Production apply checklist:
--   * additive tables/indexes only
--   * RLS enabled
--   * no anon/authenticated/PUBLIC grants
--   * clinic_runtime least privilege

begin;

create table if not exists provenance.dental_sales_tags (
  clinic_id bigint not null references public.clinics(id),
  tag_code text not null,
  tag_label text not null,
  priority_group smallint not null check (priority_group between 1 and 9),
  sort_order integer not null,
  auto_status text not null check (auto_status in ('CONFIRMED','REVIEW')),
  confidence double precision not null check (confidence >= 0 and confidence <= 1),
  source text not null,
  matched_alias text not null default '',
  evidence_text text not null default '',
  rule_id text not null,
  classifier_version text not null,
  active boolean not null default true,
  computed_at timestamptz not null default now(),
  primary key(clinic_id,tag_code)
);

create index if not exists dental_sales_tags_priority_idx
  on provenance.dental_sales_tags(priority_group,sort_order,clinic_id)
  where active=true and auto_status='CONFIRMED';

create index if not exists dental_sales_tags_code_idx
  on provenance.dental_sales_tags(tag_code,clinic_id)
  where active=true;

create table if not exists provenance.dental_sales_tag_reviews (
  id text primary key,
  clinic_id bigint not null references public.clinics(id),
  tag_code text not null,
  human_decision text not null
    check (human_decision in ('CONFIRMED','REJECTED','UNCERTAIN')),
  reviewer text not null,
  review_note text not null default '',
  auto_snapshot jsonb not null,
  reviewed_at timestamptz not null default now()
);

create index if not exists dental_sales_tag_reviews_latest_idx
  on provenance.dental_sales_tag_reviews(clinic_id,tag_code,reviewed_at desc,id desc);

alter table provenance.dental_sales_tags enable row level security;
alter table provenance.dental_sales_tag_reviews enable row level security;

grant select,insert,update on provenance.dental_sales_tags to clinic_runtime;
grant select,insert on provenance.dental_sales_tag_reviews to clinic_runtime;
revoke all on provenance.dental_sales_tags from anon,authenticated,public;
revoke all on provenance.dental_sales_tag_reviews from anon,authenticated,public;

do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname='provenance'
      and tablename='dental_sales_tags'
      and policyname='clinic_runtime_dental_sales_tags_select'
  ) then
    create policy clinic_runtime_dental_sales_tags_select
      on provenance.dental_sales_tags for select to clinic_runtime using (true);
  end if;

  if not exists (
    select 1 from pg_policies
    where schemaname='provenance'
      and tablename='dental_sales_tags'
      and policyname='clinic_runtime_dental_sales_tags_insert'
  ) then
    create policy clinic_runtime_dental_sales_tags_insert
      on provenance.dental_sales_tags for insert to clinic_runtime with check (true);
  end if;

  if not exists (
    select 1 from pg_policies
    where schemaname='provenance'
      and tablename='dental_sales_tags'
      and policyname='clinic_runtime_dental_sales_tags_update'
  ) then
    create policy clinic_runtime_dental_sales_tags_update
      on provenance.dental_sales_tags for update to clinic_runtime
      using (true) with check (true);
  end if;

  if not exists (
    select 1 from pg_policies
    where schemaname='provenance'
      and tablename='dental_sales_tag_reviews'
      and policyname='clinic_runtime_dental_sales_tag_reviews_select'
  ) then
    create policy clinic_runtime_dental_sales_tag_reviews_select
      on provenance.dental_sales_tag_reviews for select to clinic_runtime using (true);
  end if;

  if not exists (
    select 1 from pg_policies
    where schemaname='provenance'
      and tablename='dental_sales_tag_reviews'
      and policyname='clinic_runtime_dental_sales_tag_reviews_insert'
  ) then
    create policy clinic_runtime_dental_sales_tag_reviews_insert
      on provenance.dental_sales_tag_reviews for insert to clinic_runtime with check (true);
  end if;
end $$;

commit;

-- Read-only validation:
-- select to_regclass('provenance.dental_sales_tags'),
--        to_regclass('provenance.dental_sales_tag_reviews');
-- select grantee,table_name,privilege_type
-- from information_schema.table_privileges
-- where table_schema='provenance'
--   and table_name in ('dental_sales_tags','dental_sales_tag_reviews')
-- order by table_name,grantee,privilege_type;
