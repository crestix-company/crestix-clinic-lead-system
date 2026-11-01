-- Comdesk import safety guard.
-- Existing clinic UUIDs are not Comdesk UUIDs. Every writable case row must carry
-- an independently mapped case ID and a confirmed project scope in record_json.
-- This migration is a proposal only; review existing duplicates before applying.
CREATE UNIQUE INDEX IF NOT EXISTS comdesk_project_case_unique_idx
ON provenance.source_records (
    (BTRIM(record_json->>'comdesk_project_id')),
    (BTRIM(record_json->>'comdesk_case_id'))
)
WHERE source = 'コムデスク'
  AND NULLIF(BTRIM(record_json->>'comdesk_project_id'), '') IS NOT NULL
  AND NULLIF(BTRIM(record_json->>'comdesk_case_id'), '') IS NOT NULL;
