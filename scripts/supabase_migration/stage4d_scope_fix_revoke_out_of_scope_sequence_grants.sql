-- Stage4-D Live Gate 1 corrective fix -- applied live immediately after
-- stage4d_runtime_write_schema.sql's first apply, same session, same day.
--
-- stage4d_runtime_write_schema.sql's sequence-USAGE-grant loop originally matched on schema
-- (public/provenance/research) rather than the exact 7 sequences the Gate1.5 mandatory scope
-- actually needs, so it also granted USAGE on 4 pre-existing sequences belonging to tables
-- outside that scope: public.clinic_email_enrichment, public.new_clinic_candidates,
-- public.new_clinic_candidate_evidence, public.new_clinic_source_records.
--
-- This was caught during the Live DDL Gate's catalog verification step (before any WRITE
-- cutover, canary, or data mutation) -- not after the fact. No table-level INSERT/UPDATE/DELETE
-- grant or RLS policy was ever given to clinic_runtime on those 4 tables, so this was never an
-- actual data-access path; it is revoked purely on the "minimal necessary grant only" principle.
--
-- stage4d_runtime_write_schema.sql's sequence loop has since been corrected to enumerate the
-- exact 7 intended sequences explicitly, so a fresh apply of that file will not repeat this.
-- This file is kept as the historical record of the exact corrective SQL run against the live
-- project, so Git and the live database stay consistent (no live-only unmanaged change).

revoke usage, select on sequence
  public.clinic_email_enrichment_id_seq,
  public.new_clinic_candidates_id_seq,
  public.new_clinic_candidate_evidence_id_seq,
  public.new_clinic_source_records_id_seq
from clinic_runtime;
