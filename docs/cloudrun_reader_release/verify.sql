-- SELECT only, after approved application. All *_ok columns must be true.
SELECT
 NOT has_any_column_privilege('clinic_cloudrun_reader','public.clinics','SELECT') AS clinics_denied_ok,
 NOT has_column_privilege('clinic_cloudrun_reader','public.clinics','effective_json','SELECT') AS raw_effective_denied_ok,
 has_column_privilege('clinic_cloudrun_reader','public.clinic_cloudrun_readonly','phone_norm','SELECT') AS phone_norm_select_ok,
 NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public'
   AND table_name='clinic_cloudrun_readonly' AND column_name='tel_match_key') AS tel_match_key_absent_ok,
 NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public'
   AND table_name='clinic_cloudrun_readonly' AND column_name='epark_contract') AS epark_contract_column_absent_ok,
 NOT has_any_column_privilege('clinic_cloudrun_reader','provenance.comdesk_original_rows','SELECT') AS raw_comdesk_denied_ok,
 NOT has_schema_privilege('clinic_cloudrun_projection_owner','public','CREATE') AS helper_public_create_denied_ok,
 NOT has_schema_privilege('clinic_cloudrun_projection_owner','provenance','CREATE') AS helper_provenance_create_denied_ok;
SELECT rolname,NOT rolcanlogin AND NOT rolsuper AND NOT rolbypassrls
  AND NOT rolcreatedb AND NOT rolcreaterole AND NOT rolreplication AS helper_flags_ok
FROM pg_roles WHERE rolname='clinic_cloudrun_projection_owner';
SELECT NOT EXISTS (SELECT 1 FROM pg_auth_members m
  WHERE m.member IN (SELECT oid FROM pg_roles WHERE rolname IN ('clinic_cloudrun_reader','clinic_cloudrun_projection_owner'))
     OR (m.roleid=(SELECT oid FROM pg_roles WHERE rolname='clinic_cloudrun_projection_owner')
         AND NOT (m.member=(SELECT oid FROM pg_roles WHERE rolname='postgres')
                  AND m.admin_option AND NOT m.inherit_option AND NOT m.set_option))) AS membership_ok;
SELECT n.nspname,c.relname,pg_get_userbyid(c.relowner)='clinic_cloudrun_projection_owner' AS owner_ok,
       'security_barrier=true'=ANY(c.reloptions) AS security_barrier_ok,
       NOT EXISTS (SELECT 1 FROM aclexplode(c.relacl) a
          WHERE a.grantee<>c.relowner AND
          (a.grantee<>(SELECT oid FROM pg_roles WHERE rolname='clinic_cloudrun_reader')
           OR a.privilege_type<>'SELECT' OR a.is_grantable)) AS view_acl_ok
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE c.oid IN ('public.clinic_cloudrun_readonly'::regclass,'provenance.clinic_cloudrun_comdesk_rows'::regclass);
SELECT COALESCE((SELECT format_type(a.atttypid,a.atttypmod)='text'
  FROM pg_attribute a WHERE a.attrelid='public.clinic_cloudrun_readonly'::regclass
    AND a.attname='phone_norm' AND a.attnum>0 AND NOT a.attisdropped),false)
  AS phone_norm_type_ok;
SELECT
  NOT EXISTS (SELECT 1 FROM public.clinic_cloudrun_readonly
              WHERE effective_projection ? 'epark_contract')
  AND NOT EXISTS (
    SELECT 1 FROM public.clinic_cloudrun_readonly v
    CROSS JOIN LATERAL jsonb_array_elements_text(v.signals_json) s(value)
    WHERE s.value='EPARK課金済み確認'
  ) AS epark_charge_hidden_ok,
  NOT EXISTS (
    SELECT 1 FROM public.clinic_cloudrun_readonly v
    WHERE v.signal_count<>jsonb_array_length(v.signals_json)
       OR v.hot_status<>CASE WHEN jsonb_array_length(v.signals_json)>=3 THEN 'かなりアツい'
                             WHEN jsonb_array_length(v.signals_json)=2 THEN 'アツい'
                             WHEN jsonb_array_length(v.signals_json)=1 THEN '集客投資シグナルあり'
                             ELSE '通常' END
  ) AS signal_aggregate_consistent_ok;
SELECT p.proname,NOT p.prosecdef AS invoker_ok,
       NOT EXISTS (SELECT 1 FROM aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
         WHERE a.grantee<>p.proowner AND a.grantee NOT IN
         (SELECT oid FROM pg_roles WHERE rolname IN ('clinic_cloudrun_reader','clinic_cloudrun_projection_owner'))) AS function_acl_ok
FROM pg_proc p WHERE p.oid='public.clinic_cloudrun_effective_projection(text,bigint)'::regprocedure;
SELECT n.nspname,c.relname,
       NOT has_any_column_privilege('clinic_cloudrun_reader',c.oid,'INSERT')
       AND NOT has_any_column_privilege('clinic_cloudrun_reader',c.oid,'UPDATE')
       AND NOT has_table_privilege('clinic_cloudrun_reader',c.oid,'DELETE')
       AND NOT has_table_privilege('clinic_cloudrun_reader',c.oid,'TRUNCATE') AS no_write_ok,
       CASE WHEN c.oid IN ('public.clinics'::regclass,'provenance.comdesk_original_rows'::regclass)
         THEN NOT has_any_column_privilege('clinic_cloudrun_reader',c.oid,'SELECT')
         ELSE has_any_column_privilege('clinic_cloudrun_reader',c.oid,'SELECT') END AS read_boundary_ok
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE (n.nspname,c.relname) IN
  (('public','clinics'),('public','clinic_cloudrun_readonly'),
   ('provenance','comdesk_original_rows'),('provenance','clinic_cloudrun_comdesk_rows'),
   ('provenance','templates'),('hp_research','clinic_hp_research'),
   ('treatment','clinic_treatment_research'),('treatment','clinic_research_status'),
   ('public','clinic_email_enrichment'));
