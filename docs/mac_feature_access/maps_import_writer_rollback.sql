-- PROPOSAL ONLY. Run only if the Maps role/policies have been approved and applied.
-- This rollback removes only the dedicated Maps role's policies, grants, and role.
-- It does not touch clinic data, UUIDs, import rows, or other roles.

BEGIN;

DROP POLICY IF EXISTS clinic_maps_import_history_insert ON provenance.change_history;
DROP POLICY IF EXISTS clinic_maps_import_results_insert ON provenance.google_maps_results;
DROP POLICY IF EXISTS clinic_maps_import_batches_insert ON provenance.import_batches;
DROP POLICY IF EXISTS clinic_maps_import_batches_select ON provenance.import_batches;
DROP POLICY IF EXISTS clinic_maps_import_writer_update ON public.clinics;
DROP POLICY IF EXISTS clinic_maps_import_reader_select ON public.clinics;

REVOKE USAGE ON SEQUENCE provenance.change_history_id_seq FROM clinic_maps_import_writer;
REVOKE USAGE ON SEQUENCE provenance.google_maps_results_id_seq FROM clinic_maps_import_writer;
REVOKE INSERT (clinic_id, action, before_json, after_json, note, created_at)
    ON provenance.change_history FROM clinic_maps_import_writer;
REVOKE INSERT (clinic_id, batch_id, row_number, result_json, maps_match_status,
               maps_match_method, maps_profile_url, maps_website_url, scraped_at, created_at)
    ON provenance.google_maps_results FROM clinic_maps_import_writer;
REVOKE INSERT (id, source, result_json, created_at), SELECT (id, result_json)
    ON provenance.import_batches FROM clinic_maps_import_writer;
REVOKE UPDATE (base_json, effective_json, maps_presence_status, maps_profile_url,
               maps_website_url, maps_match_method, maps_checked_at, exclude_reason),
       SELECT (id, merged_into, base_json, effective_json, tel_match_key, name_norm, address_norm)
    ON public.clinics FROM clinic_maps_import_writer;
REVOKE USAGE ON SCHEMA public, provenance FROM clinic_maps_import_writer;
REVOKE CONNECT ON DATABASE postgres FROM clinic_maps_import_writer;

DROP ROLE clinic_maps_import_writer;

COMMIT;
