-- SELECT only. No secrets, password hashes, clinic rows or connection strings are returned.
SELECT rolname,rolcanlogin,rolinherit,rolsuper,rolcreatedb,rolcreaterole,rolreplication,
       rolbypassrls,rolvaliduntil FROM pg_roles
WHERE rolname IN ('clinic_cloudrun_reader','clinic_cloudrun_projection_owner');
SELECT r.rolname AS granted_role,u.rolname AS member_role,m.admin_option,m.inherit_option,m.set_option
FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.roleid JOIN pg_roles u ON u.oid=m.member
WHERE r.rolname IN ('clinic_cloudrun_reader','clinic_cloudrun_projection_owner')
   OR u.rolname IN ('clinic_cloudrun_reader','clinic_cloudrun_projection_owner');
SELECT n.nspname,c.relname,c.relkind,c.relrowsecurity,c.relforcerowsecurity,
       pg_get_userbyid(c.relowner) AS owner,c.reloptions,c.relacl
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE (n.nspname,c.relname) IN
  (('public','clinics'),('public','clinic_cloudrun_readonly'),
   ('provenance','comdesk_original_rows'),('provenance','clinic_cloudrun_comdesk_rows'),
   ('provenance','templates'),('hp_research','clinic_hp_research'),
   ('treatment','clinic_treatment_research'),('treatment','clinic_research_status'),
   ('public','clinic_email_enrichment'));
SELECT schemaname,tablename,policyname,permissive,roles,cmd,qual,with_check
FROM pg_policies WHERE (schemaname,tablename) IN
  (('public','clinics'),('provenance','comdesk_original_rows'),('provenance','templates'),
   ('hp_research','clinic_hp_research'),('treatment','clinic_treatment_research'),
   ('treatment','clinic_research_status'),('public','clinic_email_enrichment'));
SELECT n.nspname,c.relname,
  has_table_privilege('clinic_cloudrun_reader',c.oid,'SELECT') AS table_select,
  has_any_column_privilege('clinic_cloudrun_reader',c.oid,'SELECT') AS column_select,
  has_any_column_privilege('clinic_cloudrun_reader',c.oid,'INSERT') AS insert_access,
  has_any_column_privilege('clinic_cloudrun_reader',c.oid,'UPDATE') AS update_access,
  has_table_privilege('clinic_cloudrun_reader',c.oid,'DELETE') AS delete_access,
  has_table_privilege('clinic_cloudrun_reader',c.oid,'TRUNCATE') AS truncate_access
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname IN ('public','provenance','hp_research','treatment','research')
  AND c.relkind IN ('r','p','v','m','f') ORDER BY 1,2;
SELECT n.nspname,c.relname,a.attname,format_type(a.atttypid,a.atttypmod) AS type,
       has_column_privilege('clinic_cloudrun_reader',c.oid,a.attnum,'SELECT') AS reader_select
FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE a.attnum>0 AND NOT a.attisdropped AND (n.nspname,c.relname) IN
  (('public','clinics'),('public','clinic_cloudrun_readonly'),
   ('provenance','comdesk_original_rows'),('provenance','clinic_cloudrun_comdesk_rows'),
   ('provenance','templates'),('hp_research','clinic_hp_research'),
   ('treatment','clinic_treatment_research'),('treatment','clinic_research_status'),
   ('public','clinic_email_enrichment')) ORDER BY 1,2,a.attnum;
SELECT pg_get_userbyid(d.defaclrole) AS creator,n.nspname,d.defaclobjtype,d.defaclacl
FROM pg_default_acl d LEFT JOIN pg_namespace n ON n.oid=d.defaclnamespace
WHERE d.defaclnamespace=0 OR n.nspname IN ('public','provenance');
SELECT nspname,nspacl FROM pg_namespace
WHERE nspname IN ('public','provenance','hp_research','treatment');
SELECT p.proname,pg_get_function_identity_arguments(p.oid),p.prosecdef,p.proacl,
       pg_get_userbyid(p.proowner) AS owner
FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
WHERE n.nspname='public' AND p.proname='clinic_cloudrun_effective_projection';
