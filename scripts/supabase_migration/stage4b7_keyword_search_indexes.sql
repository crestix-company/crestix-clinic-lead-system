-- Stage4-B.7 Targeted Performance Closure
--
-- Reproduces the pg_trgm extension and expression indexes verified in the live
-- Supabase catalog on 2026-10-06. This file is intentionally idempotent. It was
-- added for source control only and must not be reapplied to the already-migrated
-- live database as part of Stage4-B.7 closure.

CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;

CREATE INDEX IF NOT EXISTS idx_clinics_name_ascii_fold_trgm
ON public.clinics USING gin (
    translate(
        clinic_name,
        'ABCDEFGHIJKLMNOPQRSTUVWXYZ'::text,
        'abcdefghijklmnopqrstuvwxyz'::text
    ) gin_trgm_ops
);

CREATE INDEX IF NOT EXISTS idx_clinics_phone_ascii_fold_trgm
ON public.clinics USING gin (
    translate(
        phone_norm,
        'ABCDEFGHIJKLMNOPQRSTUVWXYZ'::text,
        'abcdefghijklmnopqrstuvwxyz'::text
    ) gin_trgm_ops
);
