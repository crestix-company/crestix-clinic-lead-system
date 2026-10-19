-- Read-only audit preview for an authorized operator.
-- Run against the confirmed Clinic Lead database using a dedicated role with
-- SELECT-only grants on public.clinics and provenance.comdesk_original_rows.
-- This file intentionally contains no INSERT, UPDATE, DELETE, DDL, or backfill.
BEGIN TRANSACTION READ ONLY;
SET LOCAL statement_timeout = '120s';

-- Dataset size and current linkage rate.
SELECT
  (SELECT count(*) FROM public.clinics WHERE merged_into IS NULL) AS active_clinics,
  (SELECT count(*) FROM public.clinics WHERE merged_into IS NULL AND btrim(uuid) <> '') AS active_clinics_with_uuid,
  (SELECT count(*) FROM provenance.comdesk_original_rows) AS comdesk_original_rows,
  (SELECT count(*) FROM provenance.comdesk_original_rows WHERE clinic_id IS NOT NULL) AS linked_original_rows,
  (SELECT count(*) FROM provenance.comdesk_original_rows WHERE clinic_id IS NULL) AS unlinked_original_rows;

-- Unlinked rows resolvable by exact, existing Comdesk UUID.
SELECT count(DISTINCT o.id) AS uuid_auto_match_rows
FROM provenance.comdesk_original_rows o
JOIN public.clinics c
  ON c.uuid = btrim(o.uuid)
 AND c.merged_into IS NULL
WHERE o.clinic_id IS NULL
  AND btrim(o.uuid) <> '';

-- UUIDs assigned to multiple active clinic rows (summary only; do not emit UUID values).
SELECT count(*) AS clinic_uuid_conflict_groups
FROM (
  SELECT uuid
  FROM public.clinics
  WHERE merged_into IS NULL AND btrim(uuid) <> ''
  GROUP BY uuid
  HAVING count(DISTINCT id) > 1
) conflicts;

-- Existing UUID exact match with conflicting name/address/phone evidence.
-- This is a HOLD count; comparisons are conservative text checks, not an auto-merge rule.
SELECT count(*) AS uuid_match_information_conflict_rows
FROM provenance.source_records s
JOIN public.clinics c
  ON c.uuid = btrim(COALESCE(s.record_json->>'uuid',''))
 AND c.merged_into IS NULL
WHERE s.source = 'コムデスク'
  AND btrim(COALESCE(s.record_json->>'uuid','')) <> ''
  AND (
    (btrim(COALESCE(s.record_json->>'clinic_name','')) <> ''
      AND regexp_replace(lower(btrim(s.record_json->>'clinic_name')), '[[:space:]]', '', 'g')
          <> regexp_replace(lower(btrim(c.clinic_name)), '[[:space:]]', '', 'g'))
    OR (btrim(COALESCE(s.record_json->>'address','')) <> ''
      AND regexp_replace(lower(btrim(s.record_json->>'address')), '[[:space:]]', '', 'g')
          <> regexp_replace(lower(btrim(c.address)), '[[:space:]]', '', 'g'))
    OR (btrim(COALESCE(s.record_json->>'phone','')) <> ''
      AND regexp_replace(s.record_json->>'phone', '[^0-9]', '', 'g')
          <> regexp_replace(c.phone, '[^0-9]', '', 'g'))
  );

-- Same normalized master name/address/phone across medical and dental registrations.
-- Candidate counts only; every group still needs the reviewed match policy before linkage.
SELECT count(*) AS medical_dental_exact_identity_candidate_pairs
FROM public.clinics a
JOIN public.clinics b
  ON a.id < b.id
 AND a.name_norm = b.name_norm
 AND a.address_norm = b.address_norm
 AND a.phone_norm = b.phone_norm
WHERE a.merged_into IS NULL AND b.merged_into IS NULL
  AND a.name_norm <> '' AND a.address_norm <> '' AND a.phone_norm <> ''
  AND ((a.medical_type = '医科' AND b.medical_type = '歯科')
    OR (a.medical_type = '歯科' AND b.medical_type = '医科'));

-- Conservative backfill preview categories for unlinked Comdesk originals.
-- Exact-field candidates use raw trimmed equality. Normalized-name/address candidates
-- are classified only as HUMAN_REVIEW; this query never approves or writes a link.
WITH unlinked AS (
  SELECT o.id,o.uuid,
         COALESCE(s.record_json->>'clinic_name','') AS clinic_name,
         COALESCE(s.record_json->>'address','') AS address,
         COALESCE(s.record_json->>'phone','') AS phone
  FROM provenance.comdesk_original_rows o
  LEFT JOIN provenance.source_records s
    ON s.source='コムデスク' AND s.source_hash=o.source_hash AND s.row_number=o.row_number
  WHERE o.clinic_id IS NULL
), candidate_counts AS (
  SELECT u.id,
    (SELECT count(DISTINCT c.id) FROM public.clinics c
      WHERE c.merged_into IS NULL AND btrim(u.uuid)<>'' AND c.uuid=btrim(u.uuid)) AS uuid_matches,
    (SELECT count(DISTINCT c.id) FROM public.clinics c
      WHERE c.merged_into IS NULL
        AND btrim(u.clinic_name)<>'' AND btrim(u.address)<>'' AND btrim(u.phone)<>''
        AND c.clinic_name=btrim(u.clinic_name) AND c.address=btrim(u.address) AND c.phone=btrim(u.phone)) AS exact_field_matches,
    (SELECT count(DISTINCT c.id) FROM public.clinics c
      WHERE c.merged_into IS NULL
        AND btrim(u.clinic_name)<>'' AND btrim(u.address)<>''
        AND c.name_norm=regexp_replace(lower(btrim(u.clinic_name)), '[[:space:]]', '', 'g')
        AND c.address_norm=regexp_replace(lower(btrim(u.address)), '[[:space:]]', '', 'g')) AS normalized_name_address_matches,
    (SELECT count(DISTINCT c.id) FROM public.clinics c
      WHERE c.merged_into IS NULL AND btrim(u.uuid)<>'' AND c.uuid=btrim(u.uuid)
        AND ((btrim(u.clinic_name)<>'' AND c.clinic_name<>btrim(u.clinic_name))
          OR (btrim(u.address)<>'' AND c.address<>btrim(u.address))
          OR (btrim(u.phone)<>'' AND regexp_replace(c.phone,'[^0-9]','','g')<>regexp_replace(u.phone,'[^0-9]','','g')))) AS uuid_info_conflicts
  FROM unlinked u
), classified AS (
  SELECT *, CASE
    WHEN (uuid_matches=1 AND uuid_info_conflicts=0)
      OR (uuid_matches=0 AND exact_field_matches=1) THEN 'AUTO_RESTORABLE'
    WHEN uuid_matches>1 OR uuid_info_conflicts>0 OR exact_field_matches>1
      OR normalized_name_address_matches>0 THEN 'HUMAN_REVIEW'
    ELSE 'NO_MATCH'
  END AS preview_class
  FROM candidate_counts
)
SELECT
  count(*) FILTER (WHERE preview_class='AUTO_RESTORABLE') AS preview_auto_restorable,
  count(*) FILTER (WHERE preview_class='HUMAN_REVIEW') AS preview_human_review,
  count(*) FILTER (WHERE preview_class='NO_MATCH') AS preview_no_match
FROM classified;

-- Backfill preview is totals only. No candidate rows or data are changed by this script.
ROLLBACK;
