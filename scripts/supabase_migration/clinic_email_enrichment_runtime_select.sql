-- Step 5 Comdesk export needs read-only access to already-enriched email rows.
-- This migration is intentionally limited to clinic_runtime SELECT access.

BEGIN;

GRANT SELECT ON TABLE public.clinic_email_enrichment TO clinic_runtime;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_policies
    WHERE schemaname = 'public'
      AND tablename = 'clinic_email_enrichment'
      AND policyname = 'clinic_runtime_clinic_email_enrichment_select'
  ) THEN
    CREATE POLICY clinic_runtime_clinic_email_enrichment_select
      ON public.clinic_email_enrichment
      FOR SELECT
      TO clinic_runtime
      USING (true);
  END IF;
END
$$;

COMMIT;
