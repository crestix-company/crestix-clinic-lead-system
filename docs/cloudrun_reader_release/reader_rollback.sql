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
