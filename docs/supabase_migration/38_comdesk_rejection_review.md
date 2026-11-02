# Comdesk rejected-row ledger

The Supabase Comdesk writer validates the entire upload before applying accepted rows. Invalid rows are excluded from Clinic Master and written to `provenance.comdesk_rejections` in the same transaction as accepted rows and the import-batch receipt. If either the import or rejection-ledger write fails, the transaction rolls back and the UI reports the error.

The ledger preserves the source row as a positional JSON array (so duplicate or blank CSV headers remain distinguishable), the normalized record when available, the column mapping and fixed-scope confirmation, the project scope, a source-row SHA-256, and one or more validation reason codes. Rows that cannot be matched to an existing Clinic Master record are also held rather than used to create a canonical clinic. Re-running the identical batch returns its stored receipt and does not create duplicate holds.

## Deployment

Apply `supabase/migrations/20261102100000_comdesk_rejections.sql` after reviewing the production `provenance.source_records` data and before enabling the Comdesk writer. The writer also requires the unique scoped case-ID index from `20261021100000_comdesk_project_case_unique.sql`; without it, accepted rows fail closed. These SQL migrations are not applied by the application.

The migration does not create roles or grant access to `anon`, `authenticated`, or `PUBLIC`. If `clinic_comdesk_import_writer` exists, it receives only `INSERT` on the rejection table and `USAGE` on its identity sequence. That writer must separately have the privileges needed by the existing import path. Database owners can review and update statuses; no review, retry, correction, or discard UI is included in this change. Do not grant the general reader role access to this table because it contains original uploaded rows.

## Statuses

- `OPEN`: awaiting review
- `REVIEWED`: inspected, with no resolution recorded
- `REJECTED`: intentionally discarded after review
- `RESOLVED`: corrected/reprocessed through a separately approved workflow

Reason counts returned by the import screen count validation findings; a row with multiple findings contributes to multiple reason counts but only once to the rejected-row total.
