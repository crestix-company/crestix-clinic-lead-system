-- Run ONLY through the dedicated Reader Session Pooler connection after approved DB apply.
-- Dashboard execution as postgres does not validate Reader authentication or Reader RLS.
BEGIN READ ONLY;
SELECT current_user,current_setting('transaction_read_only');
SELECT count(*) AS national_visible_rows FROM public.clinic_cloudrun_readonly;
SELECT count(*) AS forbidden_epark_charge_exposures FROM public.clinic_cloudrun_readonly v
WHERE v.effective_projection ? 'epark_contract'
   OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(v.signals_json) s(value)
              WHERE s.value='EPARK課金済み確認');
-- Resolve the column contract through the Reader role without assuming any live phone value.
SELECT phone_norm FROM public.clinic_cloudrun_readonly LIMIT 0;
SELECT prefecture,count(*) FROM public.clinic_cloudrun_readonly GROUP BY prefecture ORDER BY prefecture;
SELECT count(*) AS projected_comdesk_rows FROM provenance.clinic_cloudrun_comdesk_rows;
SELECT count(*) AS referenced_templates FROM provenance.templates;
SELECT count(*) AS invalid_email_rows FROM public.clinic_email_enrichment
WHERE status IS DISTINCT FROM 'VERIFIED_EMAIL' OR verified_on_official IS DISTINCT FROM true
   OR NULLIF(BTRIM(email),'') IS NULL;
COMMIT;
