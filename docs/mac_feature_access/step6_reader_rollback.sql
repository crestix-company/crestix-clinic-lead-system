-- REVIEW ONLY. Run only if the accompanying apply was approved and applied.
BEGIN;
DROP POLICY IF EXISTS clinic_comdesk_export_reader_select ON public.clinics;
DROP POLICY IF EXISTS clinic_comdesk_export_reader_select ON research.research_jobs;
DROP POLICY IF EXISTS clinic_comdesk_export_reader_select ON research.research_job_items;
DROP POLICY IF EXISTS clinic_comdesk_export_reader_select ON research.research_results;
DROP POLICY IF EXISTS clinic_comdesk_export_reader_select ON hp_research.clinic_hp_research;
DROP POLICY IF EXISTS clinic_comdesk_export_reader_select ON provenance.hp_human_reviews;
DROP POLICY IF EXISTS clinic_comdesk_export_reader_select ON provenance.source_records;
DROP POLICY IF EXISTS clinic_comdesk_export_reader_select ON provenance.comdesk_original_rows;
DROP POLICY IF EXISTS clinic_comdesk_export_reader_select ON provenance.match_reviews;
DROP POLICY IF EXISTS clinic_comdesk_export_reader_select ON provenance.templates;
DROP POLICY IF EXISTS clinic_comdesk_export_reader_select ON provenance.manual_overrides;
DROP POLICY IF EXISTS clinic_comdesk_export_reader_select ON provenance.change_history;
DROP POLICY IF EXISTS clinic_comdesk_export_reader_select ON public.clinic_email_enrichment;
DROP POLICY IF EXISTS clinic_comdesk_export_reader_select ON public.sales_entity_clinics;
DROP POLICY IF EXISTS clinic_comdesk_export_reader_select ON public.sales_entity_comdesk_identity;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public,research,hp_research,provenance
  FROM clinic_comdesk_export_reader;
REVOKE USAGE ON SCHEMA public,research,hp_research,provenance FROM clinic_comdesk_export_reader;
REVOKE CONNECT ON DATABASE postgres FROM clinic_comdesk_export_reader;
DROP ROLE clinic_comdesk_export_reader;
COMMIT;
