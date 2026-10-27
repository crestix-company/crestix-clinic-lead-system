-- Repair an already-created Step6 Reader's three missing View column grants.
-- Run in Supabase SQL Editor as postgres, as one transaction.
BEGIN;
GRANT clinic_cloudrun_projection_owner TO postgres
  WITH ADMIN FALSE, INHERIT FALSE, SET TRUE;
SET LOCAL ROLE clinic_cloudrun_projection_owner;
GRANT SELECT (id,hp_url,maps_website_url) ON TABLE public.clinic_cloudrun_readonly
  TO clinic_comdesk_export_reader;
RESET ROLE;
REVOKE clinic_cloudrun_projection_owner FROM postgres GRANTED BY postgres;
COMMIT;
