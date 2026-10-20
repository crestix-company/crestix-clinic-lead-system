-- REVIEW PROPOSAL ONLY. Do not apply without explicit production approval.
-- Pinned PR #122 head: 74d89afee68fd6c86bc44b042048f6f618b43c39
-- Projection SHA256: f9e20f3b5e6b16c12ffbb828750bcb9fe0687b1ad12b822ea38c0bcff97c7207
-- Apply this combined file ONCE; do not separately apply the pinned migration.
BEGIN;
GRANT clinic_cloudrun_projection_owner TO postgres WITH INHERIT TRUE, SET TRUE;
-- Only policies/column grants introduced by this package. Never restore raw source access.
-- Baseline preflight must prove these grants/policies and schema USAGE did not pre-exist.
DROP POLICY clinic_cloudrun_reader_select ON hp_research.clinic_hp_research;
DROP POLICY clinic_cloudrun_reader_select ON treatment.clinic_treatment_research;
DROP POLICY clinic_cloudrun_reader_select ON treatment.clinic_research_status;
DROP POLICY clinic_cloudrun_reader_select ON provenance.templates;
DROP POLICY clinic_cloudrun_reader_select ON public.clinic_email_enrichment;
REVOKE SELECT (clinic_id,machine_hp_rank,fetch_status,hp_url,final_url,treatment_categories)
  ON hp_research.clinic_hp_research FROM clinic_cloudrun_reader;
REVOKE SELECT (clinic_id,research_status,treatment_category_name)
  ON treatment.clinic_treatment_research FROM clinic_cloudrun_reader;
REVOKE SELECT (clinic_id,research_status)
  ON treatment.clinic_research_status FROM clinic_cloudrun_reader;
REVOKE SELECT (id,headers_json,mapping_json) ON provenance.templates FROM clinic_cloudrun_reader;
REVOKE SELECT (clinic_id,email,status,verified_on_official)
  ON public.clinic_email_enrichment FROM clinic_cloudrun_reader;
REVOKE USAGE ON SCHEMA hp_research,treatment,provenance FROM clinic_cloudrun_reader;


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
  signal_count,hot_status,departments_json,treatments_json,signals_json,first_seen_at,
  last_seen_at,source_as_of_date,is_new,merged_into,merge_hold,
  maps_presence_status,maps_profile_url,maps_website_url,exclude_reason,effective_json)
  ON public.clinics FROM clinic_cloudrun_projection_owner;
REVOKE SELECT (id,clinic_id,template_id,uuid,row_json)
  ON provenance.comdesk_original_rows FROM clinic_cloudrun_projection_owner;
REVOKE SELECT (id,headers_json) ON provenance.templates FROM clinic_cloudrun_projection_owner;
REVOKE USAGE ON SCHEMA provenance FROM clinic_cloudrun_projection_owner;
DROP ROLE clinic_cloudrun_projection_owner;
COMMIT;
