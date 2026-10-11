-- Dedicated Email Research runtime role. Prepared offline; DO NOT apply to Production.
--
-- clinic_runtime must never be reused for Email Research: Email Research is a distinct
-- write surface (email_research.*, provenance.email_human_reviews) and must not inherit
-- clinic_runtime's Clinic/HP/Comdesk write privileges, nor vice versa. This file only
-- creates the role and grants it the minimum cross-schema READ access it needs to perform
-- live eligibility checks against Clinic/HP state in same_database mode; the WRITE-capable
-- grants on email_research.* / provenance.email_human_reviews are issued by
-- email_research_schema.sql / email_human_review_schema.sql (co-located with the schemas
-- they protect, same as the existing clinic_runtime pattern).
--
-- Apply this file before email_research_schema.sql / email_human_review_schema.sql.

begin;

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'clinic_email_runtime') then
    create role clinic_email_runtime nologin;
  end if;
end $$;

-- Cross-schema SELECT-only surface. Each grant is conditional on the target already
-- existing, so this file is safe to apply standalone in either topology:
--   cross_database sidecar  -- none of these relations exist in the Email DB; every
--                              branch below no-ops and clinic_email_runtime ends up with
--                              only the email_research.* / provenance grants issued later.
--   same_database integration -- public.clinics / hp_research.* / research.hp_pages /
--                              public.clinic_email_enrichment live in this same database;
--                              clinic_email_runtime gets READ ONLY access to them and
--                              nothing else. No existing grant on these shared tables is
--                              touched -- only an additional grant to this new role.
do $$
begin
  if to_regclass('public.clinics') is not null then
    execute 'grant usage on schema public to clinic_email_runtime';
    execute 'grant select on table public.clinics to clinic_email_runtime';
  end if;
  if to_regclass('public.clinic_email_enrichment') is not null then
    execute 'grant usage on schema public to clinic_email_runtime';
    execute 'grant select on table public.clinic_email_enrichment to clinic_email_runtime';
  end if;
  if to_regnamespace('hp_research') is not null then
    execute 'grant usage on schema hp_research to clinic_email_runtime';
    if to_regclass('hp_research.clinic_hp_research') is not null then
      execute 'grant select on table hp_research.clinic_hp_research to clinic_email_runtime';
    end if;
  end if;
  if to_regnamespace('research') is not null then
    execute 'grant usage on schema research to clinic_email_runtime';
    if to_regclass('research.hp_pages') is not null then
      execute 'grant select on table research.hp_pages to clinic_email_runtime';
    end if;
  end if;
end $$;

commit;

-- Read-only verification after a separately approved apply:
-- select rolname,rolcanlogin from pg_roles where rolname='clinic_email_runtime';
-- select table_schema,table_name,privilege_type from information_schema.role_table_grants
--   where grantee='clinic_email_runtime' order by 1,2,3;
