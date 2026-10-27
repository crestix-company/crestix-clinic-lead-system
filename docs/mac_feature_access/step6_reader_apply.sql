-- REVIEW ONLY. Do not apply without production DB approval.
-- The operator provisions the password out of band; this file contains no credential.
BEGIN;

DO $guard$
DECLARE item record;
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='clinic_comdesk_export_reader') THEN
    RAISE EXCEPTION 'clinic_comdesk_export_reader already exists; audit it before applying';
  END IF;
  FOR item IN SELECT * FROM (VALUES
    ('public','clinics'), ('public','clinic_email_enrichment'),
    ('public','sales_entity_clinics'), ('public','sales_entity_comdesk_identity'),
    ('research','research_jobs'), ('research','research_job_items'),
    ('research','research_results'), ('hp_research','clinic_hp_research'),
    ('provenance','hp_human_reviews'), ('provenance','source_records'),
    ('provenance','comdesk_original_rows'), ('provenance','match_reviews'),
    ('provenance','templates'), ('provenance','manual_overrides'),
    ('provenance','change_history')
  ) AS required(schema_name, table_name) LOOP
    IF NOT EXISTS (
      SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
      WHERE n.nspname=item.schema_name AND c.relname=item.table_name
        AND c.relkind IN ('r','p') AND c.relrowsecurity
    ) THEN
      RAISE EXCEPTION 'required table missing or RLS disabled: %.%', item.schema_name,item.table_name;
    END IF;
  END LOOP;
END $guard$;

CREATE ROLE clinic_comdesk_export_reader LOGIN NOINHERIT NOBYPASSRLS;
GRANT CONNECT ON DATABASE postgres TO clinic_comdesk_export_reader;
GRANT USAGE ON SCHEMA public,research,hp_research,provenance TO clinic_comdesk_export_reader;
-- The Supabase SQL executor is postgres, an ADMIN member of the View owner role
-- but with SET FALSE. Temporarily add a non-admin SET membership, grant as the
-- actual owner, then remove only this transaction's membership.
GRANT clinic_cloudrun_projection_owner TO postgres
  WITH ADMIN FALSE, INHERIT FALSE, SET TRUE;
SET LOCAL ROLE clinic_cloudrun_projection_owner;
GRANT SELECT (id,hp_url,maps_website_url) ON TABLE public.clinic_cloudrun_readonly
  TO clinic_comdesk_export_reader;
RESET ROLE;
REVOKE clinic_cloudrun_projection_owner FROM postgres GRANTED BY postgres;

-- Column grants follow COMDESK_READER_COLUMNS in src/repository/mac_feature_access.py.
-- effective_json/base_json and result_json contain broad source data. The dedicated
-- role can read those columns because the existing 30-column export and Step7 diff
-- parser require them. They are intentionally never granted to clinic_cloudrun_reader.
GRANT SELECT (id,uuid,merged_into,merge_hold,active,exclude_reason,effective_json,
  clinic_name,base_json,tel_match_key,first_seen_at,last_seen_at,source_as_of_date,is_new,
  departments_json,hp_status,hp_url,hp_rank,maps_website_url,maps_presence_status)
  ON TABLE public.clinics TO clinic_comdesk_export_reader;
GRANT SELECT (id,kind,status,created_at) ON TABLE research.research_jobs TO clinic_comdesk_export_reader;
GRANT SELECT (job_id,clinic_id,state,result) ON TABLE research.research_job_items TO clinic_comdesk_export_reader;
GRANT SELECT (clinic_id,result_json) ON TABLE research.research_results TO clinic_comdesk_export_reader;
GRANT SELECT (clinic_id,machine_hp_rank,fetch_status,hp_url,final_url,treatment_categories)
  ON TABLE hp_research.clinic_hp_research TO clinic_comdesk_export_reader;
GRANT SELECT (id,clinic_id,research_job_id,human_decision,reviewed_at)
  ON TABLE provenance.hp_human_reviews TO clinic_comdesk_export_reader;
GRANT SELECT (id,clinic_id,source) ON TABLE provenance.source_records TO clinic_comdesk_export_reader;
GRANT SELECT (id,clinic_id,uuid,row_json,template_id)
  ON TABLE provenance.comdesk_original_rows TO clinic_comdesk_export_reader;
GRANT SELECT (source_record_id,status,candidates_json)
  ON TABLE provenance.match_reviews TO clinic_comdesk_export_reader;
GRANT SELECT (id,headers_json,mapping_json) ON TABLE provenance.templates TO clinic_comdesk_export_reader;
GRANT SELECT (clinic_id,field) ON TABLE provenance.manual_overrides TO clinic_comdesk_export_reader;
GRANT SELECT (id) ON TABLE provenance.change_history TO clinic_comdesk_export_reader;
GRANT SELECT (clinic_id,email,status,verified_on_official)
  ON TABLE public.clinic_email_enrichment TO clinic_comdesk_export_reader;
GRANT SELECT (clinic_id,common_clinic_id)
  ON TABLE public.sales_entity_clinics TO clinic_comdesk_export_reader;
GRANT SELECT (common_clinic_id,comdesk_uuid)
  ON TABLE public.sales_entity_comdesk_identity TO clinic_comdesk_export_reader;

CREATE POLICY clinic_comdesk_export_reader_select ON public.clinics
  FOR SELECT TO clinic_comdesk_export_reader USING (true);
CREATE POLICY clinic_comdesk_export_reader_select ON research.research_jobs
  FOR SELECT TO clinic_comdesk_export_reader USING (kind='hp');
CREATE POLICY clinic_comdesk_export_reader_select ON research.research_job_items
  FOR SELECT TO clinic_comdesk_export_reader USING (
    EXISTS (SELECT 1 FROM research.research_jobs j WHERE j.id=job_id AND j.kind='hp')
  );
CREATE POLICY clinic_comdesk_export_reader_select ON research.research_results
  FOR SELECT TO clinic_comdesk_export_reader USING (
    EXISTS (SELECT 1 FROM research.research_job_items i WHERE i.clinic_id=research_results.clinic_id)
  );
CREATE POLICY clinic_comdesk_export_reader_select ON hp_research.clinic_hp_research
  FOR SELECT TO clinic_comdesk_export_reader USING (true);
CREATE POLICY clinic_comdesk_export_reader_select ON provenance.hp_human_reviews
  FOR SELECT TO clinic_comdesk_export_reader USING (true);
CREATE POLICY clinic_comdesk_export_reader_select ON provenance.source_records
  FOR SELECT TO clinic_comdesk_export_reader USING (source='コムデスク');
CREATE POLICY clinic_comdesk_export_reader_select ON provenance.comdesk_original_rows
  FOR SELECT TO clinic_comdesk_export_reader USING (true);
CREATE POLICY clinic_comdesk_export_reader_select ON provenance.match_reviews
  FOR SELECT TO clinic_comdesk_export_reader USING (
    EXISTS (SELECT 1 FROM provenance.source_records s
            WHERE s.id=source_record_id AND s.source='コムデスク')
  );
CREATE POLICY clinic_comdesk_export_reader_select ON provenance.templates
  FOR SELECT TO clinic_comdesk_export_reader USING (true);
CREATE POLICY clinic_comdesk_export_reader_select ON provenance.manual_overrides
  FOR SELECT TO clinic_comdesk_export_reader USING (field='hp_rank');
CREATE POLICY clinic_comdesk_export_reader_select ON provenance.change_history
  FOR SELECT TO clinic_comdesk_export_reader USING (true);
CREATE POLICY clinic_comdesk_export_reader_select ON public.clinic_email_enrichment
  FOR SELECT TO clinic_comdesk_export_reader
  USING (status='VERIFIED_EMAIL' AND verified_on_official=true);
CREATE POLICY clinic_comdesk_export_reader_select ON public.sales_entity_clinics
  FOR SELECT TO clinic_comdesk_export_reader USING (true);
CREATE POLICY clinic_comdesk_export_reader_select ON public.sales_entity_comdesk_identity
  FOR SELECT TO clinic_comdesk_export_reader USING (true);

COMMIT;
