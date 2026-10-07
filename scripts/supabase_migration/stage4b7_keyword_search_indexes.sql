-- Stage4-B.7 Targeted Performance Closure
--
-- Reproduces pg_trgm in the extensions schema and the expression indexes
-- verified in the live Supabase catalog. This file is intentionally idempotent.
-- It is a source-controlled migration definition; do not reapply it to the live
-- database after the Stage4-D security-only extension relocation has completed.

CREATE SCHEMA IF NOT EXISTS extensions;

-- New databases create pg_trgm directly in extensions. Existing databases may
-- already have it in public; relocate only when the installed extension says it
-- is relocatable, otherwise stop instead of silently leaving the Advisor warning.
CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA extensions;

DO $stage4b7_pg_trgm_schema$
DECLARE
    installed_schema name;
    can_relocate boolean;
BEGIN
    SELECT n.nspname, e.extrelocatable
      INTO installed_schema, can_relocate
      FROM pg_extension e
      JOIN pg_namespace n ON n.oid = e.extnamespace
     WHERE e.extname = 'pg_trgm';

    IF installed_schema IS NULL THEN
        RAISE EXCEPTION 'pg_trgm was not installed';
    ELSIF installed_schema <> 'extensions' THEN
        IF NOT can_relocate THEN
            RAISE EXCEPTION 'pg_trgm is installed in schema % and is not relocatable', installed_schema;
        END IF;
        EXECUTE 'ALTER EXTENSION pg_trgm SET SCHEMA extensions';
    END IF;
END
$stage4b7_pg_trgm_schema$;

CREATE INDEX IF NOT EXISTS idx_clinics_name_ascii_fold_trgm
ON public.clinics USING gin (
    translate(
        clinic_name,
        'ABCDEFGHIJKLMNOPQRSTUVWXYZ'::text,
        'abcdefghijklmnopqrstuvwxyz'::text
    ) extensions.gin_trgm_ops
);

CREATE INDEX IF NOT EXISTS idx_clinics_phone_ascii_fold_trgm
ON public.clinics USING gin (
    translate(
        phone_norm,
        'ABCDEFGHIJKLMNOPQRSTUVWXYZ'::text,
        'abcdefghijklmnopqrstuvwxyz'::text
    ) extensions.gin_trgm_ops
);
