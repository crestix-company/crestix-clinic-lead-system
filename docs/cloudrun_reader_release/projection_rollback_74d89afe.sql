-- MANUAL rollback proposal for the PR #121/#122 projection objects only.
-- DBA review required. Never run automatically or against an unverified project.
-- Direct Reader grants to public.clinics or raw Comdesk row_json are deliberately NOT restored.
BEGIN;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='clinic_cloudrun_projection_owner')
     OR (SELECT pg_get_userbyid(relowner)
         FROM pg_class WHERE oid='public.clinic_cloudrun_readonly'::regclass)
        <> 'clinic_cloudrun_projection_owner'
     OR (SELECT pg_get_userbyid(relowner)
         FROM pg_class WHERE oid='provenance.clinic_cloudrun_comdesk_rows'::regclass)
        <> 'clinic_cloudrun_projection_owner' THEN
    RAISE EXCEPTION 'Projection objects/owner differ from this change; stop rollback';
  END IF;
  IF EXISTS (
    SELECT 1 FROM pg_policy p
    WHERE (SELECT oid FROM pg_roles WHERE rolname='clinic_cloudrun_projection_owner')=ANY(p.polroles)
      AND NOT (p.polname='clinic_cloudrun_projection_owner_select'
               AND p.polrelid IN ('public.clinics'::regclass,
                                  'provenance.comdesk_original_rows'::regclass,
                                  'provenance.templates'::regclass))
  ) THEN
    RAISE EXCEPTION 'Other RLS policies depend on the projection role; stop rollback';
  END IF;
END $$;

REVOKE SELECT ON public.clinic_cloudrun_readonly FROM clinic_cloudrun_reader;
REVOKE SELECT ON provenance.clinic_cloudrun_comdesk_rows FROM clinic_cloudrun_reader;
DROP VIEW provenance.clinic_cloudrun_comdesk_rows;
DROP VIEW public.clinic_cloudrun_readonly;
DROP FUNCTION public.clinic_cloudrun_effective_projection(text,bigint);
DROP POLICY clinic_cloudrun_projection_owner_select ON public.clinics;
DROP POLICY clinic_cloudrun_projection_owner_select ON provenance.comdesk_original_rows;
DROP POLICY clinic_cloudrun_projection_owner_select ON provenance.templates;
REVOKE SELECT (id,uuid,clinic_name,phone,phone_norm,address,prefecture,medical_type,active,
  designation_date,recent_until,registration_reason,owner_equal,hp_status,hp_url,hp_rank,
  departments_json,treatments_json,signals_json,first_seen_at,
  last_seen_at,source_as_of_date,is_new,merged_into,merge_hold,
  maps_presence_status,maps_profile_url,maps_website_url,exclude_reason,effective_json)
  ON public.clinics FROM clinic_cloudrun_projection_owner;
REVOKE SELECT (id,clinic_id,template_id,uuid,row_json)
  ON provenance.comdesk_original_rows FROM clinic_cloudrun_projection_owner;
REVOKE SELECT (id,headers_json) ON provenance.templates FROM clinic_cloudrun_projection_owner;
REVOKE USAGE ON SCHEMA provenance FROM clinic_cloudrun_projection_owner;
DROP ROLE clinic_cloudrun_projection_owner;
COMMIT;
