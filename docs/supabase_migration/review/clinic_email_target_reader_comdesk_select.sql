-- REVIEW ONLY. Do not run as part of application startup or an automated migration.
-- Production execution requires separate administrator approval.
--
-- Purpose: allow the proven read-only clinic_email_target_reader role to calculate
-- Comdesk linkage state without granting access to unrelated provenance records.

\set ON_ERROR_STOP on

begin;

do $$
declare
  target_table text;
begin
  if not exists (
    select 1 from pg_roles
    where rolname = 'clinic_email_target_reader'
      and rolcanlogin
      and not rolsuper
      and not rolcreatedb
      and not rolcreaterole
      and not rolreplication
      and not rolbypassrls
  ) then
    raise exception 'expected restricted clinic_email_target_reader role is unavailable';
  end if;

  foreach target_table in array array[
    'source_records', 'comdesk_original_rows', 'match_reviews'
  ] loop
    if not exists (
      select 1
      from pg_class c
      join pg_namespace n on n.oid = c.relnamespace
      where n.nspname = 'provenance'
        and c.relname = target_table
        and c.relkind in ('r', 'p')
        and c.relrowsecurity
    ) then
      raise exception 'required provenance table is missing or RLS is disabled: %', target_table;
    end if;
  end loop;
end
$$;

grant usage on schema provenance to clinic_email_target_reader;

grant select on table
  provenance.source_records,
  provenance.comdesk_original_rows,
  provenance.match_reviews
  to clinic_email_target_reader;

do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname = 'provenance'
      and tablename = 'source_records'
      and policyname = 'clinic_email_target_reader_comdesk_sources_select'
  ) then
    create policy clinic_email_target_reader_comdesk_sources_select
      on provenance.source_records
      for select
      to clinic_email_target_reader
      using (source = 'コムデスク');
  end if;

  if not exists (
    select 1 from pg_policies
    where schemaname = 'provenance'
      and tablename = 'comdesk_original_rows'
      and policyname = 'clinic_email_target_reader_comdesk_rows_select'
  ) then
    create policy clinic_email_target_reader_comdesk_rows_select
      on provenance.comdesk_original_rows
      for select
      to clinic_email_target_reader
      using (true);
  end if;

  if not exists (
    select 1 from pg_policies
    where schemaname = 'provenance'
      and tablename = 'match_reviews'
      and policyname = 'clinic_email_target_reader_comdesk_reviews_select'
  ) then
    create policy clinic_email_target_reader_comdesk_reviews_select
      on provenance.match_reviews
      for select
      to clinic_email_target_reader
      using (
        exists (
          select 1
          from provenance.source_records s
          where s.id = source_record_id
            and s.source = 'コムデスク'
        )
      );
  end if;
end
$$;

commit;

-- Post-apply validation must be run through EMAIL_TARGET_READER_DB_URL in an
-- explicit READ ONLY transaction. Expected results:
--   * current_user = clinic_email_target_reader
--   * transaction_read_only = on
--   * SELECT = true and every write privilege = false for the three tables
--   * exactly the three role-specific SELECT policies above are applicable
-- Do not test denied writes against Production.

-- Proposed rollback, only after consumers have stopped using these permissions:
-- begin;
-- drop policy if exists clinic_email_target_reader_comdesk_reviews_select
--   on provenance.match_reviews;
-- drop policy if exists clinic_email_target_reader_comdesk_rows_select
--   on provenance.comdesk_original_rows;
-- drop policy if exists clinic_email_target_reader_comdesk_sources_select
--   on provenance.source_records;
-- revoke select on table provenance.source_records,
--   provenance.comdesk_original_rows,
--   provenance.match_reviews from clinic_email_target_reader;
-- revoke usage on schema provenance from clinic_email_target_reader;
-- commit;
