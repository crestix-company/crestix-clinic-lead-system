-- REVIEW ONLY. Do not apply without explicit production DB approval.
-- The role and the required grants/policies were observed through a READ ONLY
-- catalog audit on 2026-10-28. This is a narrow idempotent repair proposal for
-- the two relations previously reported as missing SELECT; it does not create
-- or alter the role, other relations, other policies, or any data.
BEGIN;

DO $guard$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='clinic_comdesk_export_reader') THEN
    RAISE EXCEPTION 'clinic_comdesk_export_reader does not exist';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='public' AND c.relname='clinics' AND c.relrowsecurity
  ) OR NOT EXISTS (
    SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='research' AND c.relname='research_jobs' AND c.relrowsecurity
  ) THEN
    RAISE EXCEPTION 'RLS must be enabled on both required relations';
  END IF;
END $guard$;

GRANT USAGE ON SCHEMA public,research TO clinic_comdesk_export_reader;
GRANT SELECT (
  id,uuid,merged_into,merge_hold,active,exclude_reason,effective_json,
  clinic_name,base_json,tel_match_key,first_seen_at,last_seen_at,source_as_of_date,
  is_new,departments_json,hp_status,hp_url,hp_rank,maps_website_url,maps_presence_status
) ON TABLE public.clinics TO clinic_comdesk_export_reader;
GRANT SELECT (id,kind,status,created_at)
  ON TABLE research.research_jobs TO clinic_comdesk_export_reader;

DO $policies$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policy p JOIN pg_class c ON c.oid=p.polrelid
    JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='public' AND c.relname='clinics'
      AND p.polname='clinic_comdesk_export_reader_select'
  ) THEN
    EXECUTE 'CREATE POLICY clinic_comdesk_export_reader_select ON public.clinics '
      || 'FOR SELECT TO clinic_comdesk_export_reader USING (true)';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_policy p JOIN pg_class c ON c.oid=p.polrelid
    JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='research' AND c.relname='research_jobs'
      AND p.polname='clinic_comdesk_export_reader_select'
  ) THEN
    EXECUTE 'CREATE POLICY clinic_comdesk_export_reader_select ON research.research_jobs '
      || 'FOR SELECT TO clinic_comdesk_export_reader USING (kind=''hp'')';
  END IF;
END $policies$;

COMMIT;
