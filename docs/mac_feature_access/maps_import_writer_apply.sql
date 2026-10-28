-- PROPOSAL ONLY. Not applied to Supabase.
-- Target: existing Clinic Lead Supabase project, role clinic_maps_import_writer.
-- Create the role without a password here. Set its password out-of-band using an
-- interactive administrator session (never put a password in this file or SQL history).
-- This SQL is based on the current SupabaseProvenanceWriteRepository.import_maps_results.

BEGIN;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'clinic_maps_import_writer') THEN
        RAISE EXCEPTION 'clinic_maps_import_writer already exists; audit it before applying this proposal';
    END IF;
END
$$;

CREATE ROLE clinic_maps_import_writer
    LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

GRANT CONNECT ON DATABASE postgres TO clinic_maps_import_writer;
GRANT USAGE ON SCHEMA public, provenance TO clinic_maps_import_writer;

-- Matching queries and the locked-row read. No UUID, medical_key, HP columns,
-- research schema, or manual override data is exposed to this role.
GRANT SELECT (id, merged_into, base_json, effective_json, tel_match_key, name_norm, address_norm)
    ON public.clinics TO clinic_maps_import_writer;

-- The importer updates Maps provenance plus the two existing JSON documents.
-- UUID, HP status/rank/URL, identity columns, and all non-Maps projections are
-- outside this role's column-level UPDATE grant.
GRANT UPDATE (base_json, effective_json, maps_presence_status, maps_profile_url,
              maps_website_url, maps_match_method, maps_checked_at, exclude_reason)
    ON public.clinics TO clinic_maps_import_writer;

GRANT SELECT (id, result_json) ON provenance.import_batches TO clinic_maps_import_writer;
GRANT INSERT (id, source, result_json, created_at)
    ON provenance.import_batches TO clinic_maps_import_writer;

GRANT INSERT (clinic_id, batch_id, row_number, result_json, maps_match_status,
              maps_match_method, maps_profile_url, maps_website_url, scraped_at, created_at)
    ON provenance.google_maps_results TO clinic_maps_import_writer;
GRANT USAGE ON SEQUENCE provenance.google_maps_results_id_seq TO clinic_maps_import_writer;

GRANT INSERT (clinic_id, action, before_json, after_json, note, created_at)
    ON provenance.change_history TO clinic_maps_import_writer;
GRANT USAGE ON SEQUENCE provenance.change_history_id_seq TO clinic_maps_import_writer;

CREATE POLICY clinic_maps_import_reader_select ON public.clinics
    FOR SELECT TO clinic_maps_import_writer USING (merged_into IS NULL);
CREATE POLICY clinic_maps_import_writer_update ON public.clinics
    FOR UPDATE TO clinic_maps_import_writer
    USING (merged_into IS NULL) WITH CHECK (merged_into IS NULL);

CREATE POLICY clinic_maps_import_batches_select ON provenance.import_batches
    FOR SELECT TO clinic_maps_import_writer USING (source = 'Google Maps');
CREATE POLICY clinic_maps_import_batches_insert ON provenance.import_batches
    FOR INSERT TO clinic_maps_import_writer WITH CHECK (source = 'Google Maps');

CREATE POLICY clinic_maps_import_results_insert ON provenance.google_maps_results
    FOR INSERT TO clinic_maps_import_writer
    WITH CHECK (
        clinic_id IS NULL OR EXISTS (
            SELECT 1 FROM public.clinics c
            WHERE c.id = clinic_id AND c.merged_into IS NULL
        )
    );

CREATE POLICY clinic_maps_import_history_insert ON provenance.change_history
    FOR INSERT TO clinic_maps_import_writer
    WITH CHECK (
        action = 'Google Maps取込'
        AND (clinic_id IS NULL OR EXISTS (
            SELECT 1 FROM public.clinics c
            WHERE c.id = clinic_id AND c.merged_into IS NULL
        ))
    );

COMMIT;

-- After the SQL transaction, set the role password through an interactive secure
-- administrator session. For psql, use: \password clinic_maps_import_writer
-- Then save only the dedicated role DSN in the user's mode-600 feature-writers.env.
