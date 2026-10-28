-- REVIEW ONLY. Do not apply without explicit production DB approval.
-- Provision the password separately through the database administrator's
-- password prompt. No password or DSN belongs in this file.
-- This role is only for the explicitly confirmed Step7 Apply button. The UUID
-- preview continues to use clinic_comdesk_export_reader.
BEGIN;

DO $guard$
DECLARE item record;
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='clinic_comdesk_sync_writer') THEN
    RAISE EXCEPTION 'clinic_comdesk_sync_writer already exists; audit it before applying';
  END IF;
  FOR item IN SELECT * FROM (VALUES
    ('public','clinics'),
    ('public','sales_entity_clinics'),
    ('public','sales_entity_comdesk_identity'),
    ('provenance','comdesk_original_rows'),
    ('provenance','templates'),
    ('provenance','manual_overrides'),
    ('provenance','import_batches'),
    ('provenance','change_history')
  ) AS required(schema_name,table_name) LOOP
    IF NOT EXISTS (
      SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
      WHERE n.nspname=item.schema_name AND c.relname=item.table_name
        AND c.relkind IN ('r','p') AND c.relrowsecurity
    ) THEN
      RAISE EXCEPTION 'required table missing or RLS disabled: %.%', item.schema_name,item.table_name;
    END IF;
  END LOOP;
END $guard$;

CREATE ROLE clinic_comdesk_sync_writer LOGIN NOINHERIT NOBYPASSRLS;
GRANT CONNECT ON DATABASE postgres TO clinic_comdesk_sync_writer;
GRANT USAGE ON SCHEMA public,provenance TO clinic_comdesk_sync_writer;

GRANT SELECT (id,uuid,hp_url,hp_rank,hp_status,base_json,effective_json,merged_into)
  ON TABLE public.clinics TO clinic_comdesk_sync_writer;
GRANT UPDATE (hp_url,hp_rank,hp_status,base_json,effective_json,last_seen_at)
  ON TABLE public.clinics TO clinic_comdesk_sync_writer;
GRANT SELECT (clinic_id,common_clinic_id)
  ON TABLE public.sales_entity_clinics TO clinic_comdesk_sync_writer;
GRANT SELECT (common_clinic_id,comdesk_uuid)
  ON TABLE public.sales_entity_comdesk_identity TO clinic_comdesk_sync_writer;
GRANT SELECT (uuid,clinic_id,row_json,template_id)
  ON TABLE provenance.comdesk_original_rows TO clinic_comdesk_sync_writer;
GRANT SELECT (id,headers_json) ON TABLE provenance.templates TO clinic_comdesk_sync_writer;
GRANT SELECT (clinic_id,field) ON TABLE provenance.manual_overrides TO clinic_comdesk_sync_writer;
GRANT SELECT (id,result_json), INSERT (id,source,result_json,created_at)
  ON TABLE provenance.import_batches TO clinic_comdesk_sync_writer;
GRANT INSERT (clinic_id,action,before_json,after_json,note,created_at)
  ON TABLE provenance.change_history TO clinic_comdesk_sync_writer;
GRANT USAGE ON SEQUENCE provenance.change_history_id_seq TO clinic_comdesk_sync_writer;

CREATE POLICY clinic_comdesk_sync_writer_select ON public.clinics
  FOR SELECT TO clinic_comdesk_sync_writer USING (
    merged_into IS NULL AND (
      EXISTS (SELECT 1 FROM provenance.comdesk_original_rows o WHERE o.clinic_id=clinics.id)
      OR EXISTS (
        SELECT 1 FROM public.sales_entity_clinics ec
        JOIN public.sales_entity_comdesk_identity i USING(common_clinic_id)
        WHERE ec.clinic_id=clinics.id
      )
    )
  );
CREATE POLICY clinic_comdesk_sync_writer_update ON public.clinics
  FOR UPDATE TO clinic_comdesk_sync_writer USING (
    merged_into IS NULL AND (
      EXISTS (SELECT 1 FROM provenance.comdesk_original_rows o WHERE o.clinic_id=clinics.id)
      OR EXISTS (
        SELECT 1 FROM public.sales_entity_clinics ec
        JOIN public.sales_entity_comdesk_identity i USING(common_clinic_id)
        WHERE ec.clinic_id=clinics.id
      )
    )
  ) WITH CHECK (merged_into IS NULL);
CREATE POLICY clinic_comdesk_sync_writer_select ON public.sales_entity_clinics
  FOR SELECT TO clinic_comdesk_sync_writer USING (true);
CREATE POLICY clinic_comdesk_sync_writer_select ON public.sales_entity_comdesk_identity
  FOR SELECT TO clinic_comdesk_sync_writer USING (true);
CREATE POLICY clinic_comdesk_sync_writer_select ON provenance.comdesk_original_rows
  FOR SELECT TO clinic_comdesk_sync_writer USING (true);
CREATE POLICY clinic_comdesk_sync_writer_select ON provenance.templates
  FOR SELECT TO clinic_comdesk_sync_writer USING (true);
CREATE POLICY clinic_comdesk_sync_writer_select ON provenance.manual_overrides
  FOR SELECT TO clinic_comdesk_sync_writer USING (field='hp_rank');
CREATE POLICY clinic_comdesk_sync_writer_select ON provenance.import_batches
  FOR SELECT TO clinic_comdesk_sync_writer USING (source='comdesk_sync' AND id LIKE 'comdesk_sync:%');
CREATE POLICY clinic_comdesk_sync_writer_insert ON provenance.import_batches
  FOR INSERT TO clinic_comdesk_sync_writer
  WITH CHECK (source='comdesk_sync' AND id LIKE 'comdesk_sync:%');
CREATE POLICY clinic_comdesk_sync_writer_insert ON provenance.change_history
  FOR INSERT TO clinic_comdesk_sync_writer
  WITH CHECK (
    action='Comdesk医院情報同期'
    AND EXISTS (SELECT 1 FROM public.clinics c WHERE c.id=clinic_id AND c.merged_into IS NULL)
  );

COMMIT;
