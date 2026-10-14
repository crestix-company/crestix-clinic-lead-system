-- READ ONLY access for the 「メール送付文章」 outreach Canary / recommender validation
-- (src/outreach/readonly_access.py, CLINIC_READONLY_DB_URL).
--
-- Prepared offline for DBA review. DO NOT apply to Production (hwdvyhunozporfuefteb /
-- clinic-lead-shadow) automatically -- this file is not run by any script in this repo and
-- must be applied manually (e.g. via the Supabase SQL editor) by someone with migration
-- authority, after review.
--
-- REVISION NOTE: an earlier draft of this file created a brand-new role
-- (clinic_outreach_readonly). Read-only inspection of the live schema (pg_roles,
-- information_schema.role_table_grants, pg_policies -- no data rows read, no writes) found
-- that a role named `clinic_readonly` already exists and is the evident intended target for
-- CLINIC_READONLY_DB_URL:
--   * rolcanlogin = true, rolinherit = true
--   * Zero INSERT/UPDATE/DELETE/TRUNCATE grants anywhere in the database (confirmed empty
--     result from information_schema.role_table_grants) -- genuinely read-only today.
--   * Already has GRANT SELECT + a matching permissive RLS policy ("clinic_readonly_select")
--     on exactly 2 of the 4 tables this Gate requires: public.clinics and
--     hp_research.clinic_hp_research.
--   * Missing, for the other 2 required tables (treatment.clinic_treatment_research,
--     treatment.clinic_research_status): USAGE on schema `treatment`, the SELECT grant, and
--     an RLS policy. Without the RLS policy, a SELECT grant alone is not sufficient -- both
--     tables have RLS enabled, so with no matching policy the role would authenticate but
--     read zero rows.
-- This revision extends the existing clinic_readonly role with only the missing 2-table
-- surface, instead of introducing a second, redundant read-only role.
--
-- Safe-by-construction properties:
--   * SELECT only. No INSERT/UPDATE/DELETE/TRUNCATE, no DDL, no BYPASSRLS, no schema
--     ownership, no change to the clinic_readonly role's existing grants on public.clinics /
--     hp_research.clinic_hp_research.
--   * Idempotent (IF NOT EXISTS / DO $$ catalog checks) -- safe to re-run.
--   * No grant to anon, authenticated, or PUBLIC.
--   * Does not touch clinic_runtime, clinic_email_runtime, or any other role.
--   * Password/credentials are not referenced in this file. If clinic_readonly does not
--     already have a connection string distributed for this use, set/rotate its password
--     separately via the Supabase dashboard, outside of any file, chat log, or commit.

begin;

grant usage on schema treatment to clinic_readonly;

grant select on table treatment.clinic_treatment_research to clinic_readonly;
grant select on table treatment.clinic_research_status to clinic_readonly;

do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname = 'treatment' and tablename = 'clinic_treatment_research'
      and policyname = 'clinic_readonly_select'
  ) then
    create policy clinic_readonly_select
      on treatment.clinic_treatment_research
      for select
      to clinic_readonly
      using (true);
  end if;
end $$;

do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname = 'treatment' and tablename = 'clinic_research_status'
      and policyname = 'clinic_readonly_select'
  ) then
    create policy clinic_readonly_select
      on treatment.clinic_research_status
      for select
      to clinic_readonly
      using (true);
  end if;
end $$;

-- Explicitly make the lack of write intent visible to a reviewer (REVOKE is a no-op if the
-- grants below never existed, but documents that this role must never gain them).
revoke insert, update, delete, truncate on
  treatment.clinic_treatment_research, treatment.clinic_research_status
  from clinic_readonly;

commit;

-- Read-only verification after a separately approved apply (same checks
-- src/outreach/readonly_access.verify_read_only_role() performs at runtime):
--
-- select rolname, rolcanlogin from pg_roles where rolname = 'clinic_readonly';
--
-- select table_schema, table_name, privilege_type from information_schema.role_table_grants
--   where grantee = 'clinic_readonly' order by 1, 2, 3;
-- -- expected: exactly one SELECT row for each of the 4 READ_TABLES, nothing else.
--
-- select schemaname, tablename, policyname, cmd, roles from pg_policies
--   where (schemaname, tablename) in
--     (('public','clinics'), ('hp_research','clinic_hp_research'),
--      ('treatment','clinic_treatment_research'), ('treatment','clinic_research_status'));
-- -- expected: a clinic_readonly_select / SELECT policy on all 4.
--
-- -- End-to-end (run as clinic_readonly, via its own connection string, never as a superuser):
-- select count(*) from public.clinics;                             -- expect > 0
-- select count(*) from hp_research.clinic_hp_research;              -- expect > 0
-- select count(*) from treatment.clinic_treatment_research;         -- expect > 0
-- select count(*) from treatment.clinic_research_status;            -- expect > 0
-- insert into public.clinics (id) values (-1);                      -- expect permission denied

-- ROLLBACK (if this needs to be reverted after apply):
-- begin;
-- drop policy if exists clinic_readonly_select on treatment.clinic_treatment_research;
-- drop policy if exists clinic_readonly_select on treatment.clinic_research_status;
-- revoke select on treatment.clinic_treatment_research from clinic_readonly;
-- revoke select on treatment.clinic_research_status from clinic_readonly;
-- revoke usage on schema treatment from clinic_readonly;
-- commit;
-- -- This only removes the 2-table extension added by this file. It does not touch
-- -- clinic_readonly's pre-existing access to public.clinics / hp_research.clinic_hp_research,
-- -- and does not drop the clinic_readonly role itself (it predates this file and may be used
-- -- elsewhere).
