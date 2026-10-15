-- REVIEW ONLY. This is intentionally outside supabase/migrations and has not been
-- applied to any database. Production execution requires a separately approved change.
-- Target: Supabase project hwdvyhunozporfuefteb, database postgres.
-- A secret is never embedded in this file. After applying the transaction, an
-- administrator must set the credential interactively with: psql `\password
-- clinic_email_target_reader` (do not use ALTER ROLE ... PASSWORD in shell history).

\set ON_ERROR_STOP on

begin;

do $$
begin
  if exists (
    select 1 from pg_roles where rolname = 'clinic_email_target_reader'
  ) then
    raise exception
      'clinic_email_target_reader already exists; refusing to alter an existing role';
  end if;

  if not exists (
    select 1
    from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public'
      and c.relname = 'clinics'
      and c.relkind = 'r'
      and c.relrowsecurity
  ) then
    raise exception 'public.clinics is missing or RLS is disabled';
  end if;

  if not exists (
    select 1
    from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'hp_research'
      and c.relname = 'clinic_hp_research'
      and c.relkind = 'r'
      and c.relrowsecurity
  ) then
    raise exception
      'hp_research.clinic_hp_research is missing or RLS is disabled';
  end if;
end
$$;

create role clinic_email_target_reader
  login
  noinherit
  nosuperuser
  nocreatedb
  nocreaterole
  noreplication
  nobypassrls
  password null;

alter role clinic_email_target_reader
  in database postgres
  set default_transaction_read_only = on;

grant connect on database postgres
  to clinic_email_target_reader;

grant usage on schema public, hp_research
  to clinic_email_target_reader;

grant select on table
  public.clinics,
  hp_research.clinic_hp_research
  to clinic_email_target_reader;

create policy clinic_email_target_reader_clinics_select
  on public.clinics
  for select
  to clinic_email_target_reader
  using (true);

create policy clinic_email_target_reader_hp_select
  on hp_research.clinic_hp_research
  for select
  to clinic_email_target_reader
  using (true);

commit;

-- Credential provisioning (interactive, after the transaction succeeds):
--   psql '<admin connection from the approved secret manager>'
--   \password clinic_email_target_reader
-- Then store the resulting Session Pooler URL in the organization secret manager
-- and provision EMAIL_TARGET_READER_DB_URL locally. For the shared pooler username,
-- use clinic_email_target_reader.<project-ref>; copy the exact host from Dashboard >
-- Connect > Session pooler. Do not copy the value into SQL, shell arguments, logs or Git.

-- Validation, executed only after the role credential has been provisioned:
--   select current_user, current_database(), current_setting('transaction_read_only');
-- Expected: clinic_email_target_reader, postgres, on.
--   select id, uuid from public.clinics order by id limit 1;
--   select clinic_id, researched_at from hp_research.clinic_hp_research
--     order by clinic_id limit 1;
-- Both SELECTs must succeed.
--   select table_name,
--     has_table_privilege(current_user, table_name, 'SELECT') as can_select,
--     has_table_privilege(current_user, table_name, 'INSERT') as can_insert,
--     has_table_privilege(current_user, table_name, 'UPDATE') as can_update,
--     has_table_privilege(current_user, table_name, 'DELETE') as can_delete,
--     has_table_privilege(current_user, table_name, 'TRUNCATE') as can_truncate
--   from (values ('public.clinics'),
--                ('hp_research.clinic_hp_research')) as t(table_name);
-- Expected: SELECT true, all write privileges false for both rows.
--   select n.nspname, c.relname, p.privilege
--   from pg_class c join pg_namespace n on n.oid=c.relnamespace
--   cross join (values ('INSERT'),('UPDATE'),('DELETE'),('TRUNCATE'),
--                      ('REFERENCES'),('TRIGGER')) as p(privilege)
--   where c.relkind in ('r','p','v','m','f')
--     and n.nspname not in ('pg_catalog','information_schema')
--     and has_table_privilege(current_user,c.oid,p.privilege);
-- Expected: zero rows (includes inherited and PUBLIC effective privileges).
--   select n.nspname,c.relname from pg_class c
--   join pg_namespace n on n.oid=c.relnamespace
--   where c.relkind='S'
--     and has_sequence_privilege(current_user,c.oid,'USAGE');
-- Expected: zero rows.
--   select nspname from pg_namespace
--   where nspname not like 'pg_temp_%'
--     and has_schema_privilege(current_user,oid,'CREATE');
-- Expected: zero rows.
--   select schemaname,tablename,policyname,roles,cmd
--   from pg_policies where 'clinic_email_target_reader'=any(roles);
-- Expected: exactly the two SELECT policies above.
-- Do not test writes on Production. Exercise denied INSERT/UPDATE/DELETE/TRUNCATE
-- against a disposable local/staging clone before requesting Production approval.

-- Rollback (only after all clients have been switched away and sessions are drained):
--   begin;
--   alter role clinic_email_target_reader nologin;
--   drop policy if exists clinic_email_target_reader_clinics_select
--     on public.clinics;
--   drop policy if exists clinic_email_target_reader_hp_select
--     on hp_research.clinic_hp_research;
--   revoke select on table public.clinics,
--     hp_research.clinic_hp_research from clinic_email_target_reader;
--   revoke usage on schema public,hp_research from clinic_email_target_reader;
--   revoke connect on database postgres from clinic_email_target_reader;
--   drop role clinic_email_target_reader;
--   commit;
-- Local rollback: remove EMAIL_TARGET_READER_DB_URL from the secret store and PC
-- config. Keep CLINIC_READONLY_DB_URL unchanged for Outreach and other consumers.
