-- Stage3 Google Maps Step3 matcher performance hotfix.
-- Apply only after the live safety gates in docs/supabase_migration/33_stage3_maps_performance_hotfix.md.
-- CREATE INDEX CONCURRENTLY cannot run inside an explicit transaction block.
-- This index only covers active clinics with a canonical nonblank base_json.clinic_id.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_clinics_active_base_clinic_id
    ON public.clinics ((base_json->>'clinic_id'))
    WHERE merged_into IS NULL
      AND COALESCE(base_json->>'clinic_id', '') <> '';

-- After creation, refresh planner statistics and validate the lookup plan:
-- ANALYZE public.clinics;
-- EXPLAIN (ANALYZE, BUFFERS)
-- SELECT id FROM public.clinics
-- WHERE merged_into IS NULL
--   AND COALESCE(base_json->>'clinic_id', '') <> ''
--   AND base_json->>'clinic_id' = ANY(ARRAY['sample-clinic-id']::text[]);
