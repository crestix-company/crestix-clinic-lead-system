-- Included inside apply.sql's transaction. Current catalog must match the reviewed baseline.
DO $$
DECLARE item record;
BEGIN
  IF current_user <> 'postgres' THEN
    RAISE EXCEPTION 'Use the reviewed postgres DBA context; other creators have different default ACLs';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='clinic_cloudrun_reader'
      AND rolcanlogin AND NOT rolsuper AND NOT rolbypassrls AND NOT rolcreatedb
      AND NOT rolcreaterole AND NOT rolreplication) THEN
    RAISE EXCEPTION 'Reader role flags differ from baseline';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles
                WHERE rolname='clinic_cloudrun_reader')) THEN
    RAISE EXCEPTION 'Reader has role memberships; review inherited/SET ROLE paths';
  END IF;
  IF has_schema_privilege('clinic_cloudrun_reader','hp_research','USAGE')
     OR has_schema_privilege('clinic_cloudrun_reader','treatment','USAGE')
     OR has_schema_privilege('clinic_cloudrun_reader','provenance','USAGE') THEN
    RAISE EXCEPTION 'Reader schema usage differs from audited baseline; revise scoped rollback';
  END IF;
  FOR item IN SELECT n.nspname,c.relname,c.oid,c.relrowsecurity,c.relowner
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE (n.nspname,c.relname) IN
      (('public','clinics'),('provenance','comdesk_original_rows'),('provenance','templates'),
       ('hp_research','clinic_hp_research'),('treatment','clinic_treatment_research'),
       ('treatment','clinic_research_status'),('public','clinic_email_enrichment'))
  LOOP
    IF NOT item.relrowsecurity OR pg_get_userbyid(item.relowner)<>'postgres' THEN
      RAISE EXCEPTION 'Unexpected RLS/owner on %.%',item.nspname,item.relname;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_policy WHERE polrelid=item.oid AND
      (0=ANY(polroles) OR (SELECT oid FROM pg_roles WHERE rolname='clinic_cloudrun_reader')=ANY(polroles)
       OR polname IN ('clinic_cloudrun_reader_select','clinic_cloudrun_projection_owner_select'))) THEN
      RAISE EXCEPTION 'Unexpected PUBLIC/Reader/conflicting policy on %.%',item.nspname,item.relname;
    END IF;
    IF item.oid<>'public.clinics'::regclass AND
       has_any_column_privilege('clinic_cloudrun_reader',item.oid,'SELECT') THEN
      RAISE EXCEPTION 'Existing Reader source grant differs from baseline on %.%',item.nspname,item.relname;
    END IF;
  END LOOP;
  IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
     WHERE n.nspname IN ('public','provenance','hp_research','treatment','research')
       AND c.relkind IN ('r','p','v','m','f') AND c.oid<>'public.clinics'::regclass
       AND has_any_column_privilege('clinic_cloudrun_reader',c.oid,'SELECT')) THEN
    RAISE EXCEPTION 'Reader has additional application SELECT access outside baseline';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
     WHERE n.nspname IN ('public','provenance','hp_research','treatment','research')
       AND c.relkind IN ('r','p','v','m','f') AND
       (has_table_privilege('clinic_cloudrun_reader',c.oid,'INSERT')
        OR has_any_column_privilege('clinic_cloudrun_reader',c.oid,'INSERT')
        OR has_any_column_privilege('clinic_cloudrun_reader',c.oid,'UPDATE')
        OR has_table_privilege('clinic_cloudrun_reader',c.oid,'DELETE')
        OR has_table_privilege('clinic_cloudrun_reader',c.oid,'TRUNCATE'))) THEN
    RAISE EXCEPTION 'Reader has effective application write privileges; stop';
  END IF;
END $$;
