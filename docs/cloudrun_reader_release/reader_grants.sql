-- Only five source relations remain directly readable. No source clinic/Comdesk grant.
GRANT USAGE ON SCHEMA hp_research,treatment,provenance TO clinic_cloudrun_reader;
GRANT SELECT (clinic_id,machine_hp_rank,fetch_status,hp_url,final_url,treatment_categories)
  ON hp_research.clinic_hp_research TO clinic_cloudrun_reader;
GRANT SELECT (clinic_id,research_status,treatment_category_name)
  ON treatment.clinic_treatment_research TO clinic_cloudrun_reader;
GRANT SELECT (clinic_id,research_status)
  ON treatment.clinic_research_status TO clinic_cloudrun_reader;
GRANT SELECT (id,headers_json,mapping_json) ON provenance.templates TO clinic_cloudrun_reader;
GRANT SELECT (clinic_id,email,status,verified_on_official)
  ON public.clinic_email_enrichment TO clinic_cloudrun_reader;
CREATE POLICY clinic_cloudrun_reader_select ON hp_research.clinic_hp_research
  FOR SELECT TO clinic_cloudrun_reader USING (true);
CREATE POLICY clinic_cloudrun_reader_select ON treatment.clinic_treatment_research
  FOR SELECT TO clinic_cloudrun_reader USING (true);
CREATE POLICY clinic_cloudrun_reader_select ON treatment.clinic_research_status
  FOR SELECT TO clinic_cloudrun_reader USING (true);
CREATE POLICY clinic_cloudrun_reader_select ON provenance.templates
  FOR SELECT TO clinic_cloudrun_reader USING (EXISTS (
    SELECT 1 FROM provenance.clinic_cloudrun_comdesk_rows original
    WHERE original.template_id=templates.id));
CREATE POLICY clinic_cloudrun_reader_select ON public.clinic_email_enrichment
  FOR SELECT TO clinic_cloudrun_reader USING (
    status='VERIFIED_EMAIL' AND verified_on_official IS TRUE AND NULLIF(BTRIM(email),'') IS NOT NULL);

-- Supabase default privileges also grant anon/authenticated/service_role privileges.
-- Clean only these newly-created objects; never change database-wide defaults or CRM ACLs.
DO $$
DECLARE grant_row record;
BEGIN
  FOR grant_row IN
    SELECT n.nspname,c.relname,a.grantee
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl,acldefault('r',c.relowner))) a
    WHERE c.oid IN ('public.clinic_cloudrun_readonly'::regclass,
                   'provenance.clinic_cloudrun_comdesk_rows'::regclass)
      AND a.grantee<>c.relowner
  LOOP
    EXECUTE format('REVOKE ALL ON %I.%I FROM %s',grant_row.nspname,grant_row.relname,
      CASE WHEN grant_row.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(grant_row.grantee)) END);
  END LOOP;
  FOR grant_row IN SELECT a.grantee FROM pg_proc p
    CROSS JOIN LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
    WHERE p.oid='public.clinic_cloudrun_effective_projection(text,bigint)'::regprocedure
      AND a.grantee<>p.proowner
  LOOP
    EXECUTE format('REVOKE ALL ON FUNCTION public.clinic_cloudrun_effective_projection(text,bigint) FROM %s',
      CASE WHEN grant_row.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(grant_row.grantee)) END);
  END LOOP;
END $$;
GRANT SELECT ON public.clinic_cloudrun_readonly,provenance.clinic_cloudrun_comdesk_rows
  TO clinic_cloudrun_reader;
GRANT EXECUTE ON FUNCTION public.clinic_cloudrun_effective_projection(text,bigint)
  TO clinic_cloudrun_reader,clinic_cloudrun_projection_owner;
-- Retain only the creator's ADMIN edge for scoped rollback, never data inheritance/SET ROLE.
REVOKE clinic_cloudrun_projection_owner FROM postgres GRANTED BY postgres;

DO $$
DECLARE item record;
BEGIN
  IF EXISTS (SELECT 1 FROM pg_auth_members m
    WHERE m.member IN (SELECT oid FROM pg_roles WHERE rolname IN ('clinic_cloudrun_reader','clinic_cloudrun_projection_owner'))
       OR (m.roleid=(SELECT oid FROM pg_roles WHERE rolname='clinic_cloudrun_projection_owner')
           AND NOT (m.member=(SELECT oid FROM pg_roles WHERE rolname='postgres')
                    AND m.admin_option AND NOT m.inherit_option AND NOT m.set_option))) THEN
    RAISE EXCEPTION 'Unexpected Reader/helper membership or retained temporary creator privileges';
  END IF;
  FOR item IN SELECT n.nspname,c.relname,c.oid FROM pg_class c
    JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE (n.nspname,c.relname) IN
      (('public','clinics'),('public','clinic_cloudrun_readonly'),
       ('provenance','comdesk_original_rows'),('provenance','clinic_cloudrun_comdesk_rows'),
       ('provenance','templates'),('hp_research','clinic_hp_research'),
       ('treatment','clinic_treatment_research'),('treatment','clinic_research_status'),
       ('public','clinic_email_enrichment'))
  LOOP
    IF has_any_column_privilege('clinic_cloudrun_reader',item.oid,'INSERT')
       OR has_any_column_privilege('clinic_cloudrun_reader',item.oid,'UPDATE')
       OR has_table_privilege('clinic_cloudrun_reader',item.oid,'DELETE')
       OR has_table_privilege('clinic_cloudrun_reader',item.oid,'TRUNCATE') THEN
      RAISE EXCEPTION 'Reader retains write privileges on %.%',item.nspname,item.relname;
    END IF;
  END LOOP;
  IF EXISTS (SELECT 1 FROM pg_class c,
      LATERAL aclexplode(COALESCE(c.relacl,acldefault('r',c.relowner))) a
      WHERE c.oid IN ('public.clinic_cloudrun_readonly'::regclass,
                     'provenance.clinic_cloudrun_comdesk_rows'::regclass)
        AND a.grantee<>c.relowner
        AND (a.grantee<>(SELECT oid FROM pg_roles WHERE rolname='clinic_cloudrun_reader')
             OR a.privilege_type<>'SELECT' OR a.is_grantable)) THEN
    RAISE EXCEPTION 'Unexpected new VIEW ACL';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_proc p,
      LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
      WHERE p.oid='public.clinic_cloudrun_effective_projection(text,bigint)'::regprocedure
        AND a.grantee<>p.proowner AND a.grantee NOT IN
          (SELECT oid FROM pg_roles WHERE rolname IN ('clinic_cloudrun_reader','clinic_cloudrun_projection_owner'))) THEN
    RAISE EXCEPTION 'Unexpected new function ACL';
  END IF;
END $$;
