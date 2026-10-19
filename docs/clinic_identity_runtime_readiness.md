# Common clinic identity runtime readiness

## Feature switch

`CLINIC_COMMON_IDENTITY_ENABLED` is opt-in and defaults to `0`. Both Comdesk
import and export skip common-identity queries while it is disabled. When it is
enabled, each repository still checks that the required tables exist before
using them. The production runtime config keeps the switch off until the database
and runtime-role checks below pass.

The switch is process configuration, so it can stop common-identity behavior
without changing clinic rows, UUIDs, or common-clinic mappings. Turning it off
returns Comdesk processing to the existing clinic-row matching and export path.

## Migration order

1. `20261019064657_clinic_sales_entity_identity.sql` creates the six private
   common-identity tables and the immutable UUID trigger.
2. `20261019120000_harden_clinic_sales_entity_identity_acl.sql` repairs default
   ACLs, pins the trigger function search path, and adds the audit FK index. It
   is safe to apply after the first migration has already run.
3. `20261019143000_clinic_runtime_sales_entity_access.sql` grants the existing
   `clinic_runtime` login only the operations required by the current importer
   and exporter, with RLS policies for those operations.
4. `20261019144000_clinic_runtime_audit_conflict_read.sql` adds the audit-table
   SELECT needed by the importer's idempotent `ON CONFLICT` insert.

Do not enable the feature switch until the runtime checks pass using the actual
`clinic_runtime` login. The common-identity tables contain global clinic
identity data, so the SELECT policies expose all rows only to this server-side
database role. The public, anon, and authenticated roles receive no access.

## Runtime grants

- `sales_entities`: SELECT and INSERT.
- `sales_entity_clinics`: SELECT and INSERT; column-level UPDATE on `clinic_id`
  exists only to satisfy `SELECT ... FOR UPDATE`. Its RLS UPDATE policy has
  `WITH CHECK (false)`, so row changes are rejected.
- `sales_entity_comdesk_identity`: SELECT and INSERT. UUID updates/deletes are
  not granted, and the trigger independently rejects UUID replacement.
- `sales_entity_identity_audit`: SELECT and INSERT; SELECT is required for the
  importer's idempotent `ON CONFLICT(event_key) DO NOTHING` insert.
- `sales_entity_project_memberships` and `sales_entity_resolution_queue`: no
  `clinic_runtime` grants until application code uses them.
- Sequences: USAGE only on the entity and audit sequences; no resolution-queue
  sequence access.
- DELETE, TRUNCATE, REFERENCES, and TRIGGER are not granted.

## Development verification and production cutover

1. Keep `CLINIC_COMMON_IDENTITY_ENABLED=0` while applying migrations in a
   development database.
2. Connect as `clinic_runtime` and verify the expected SELECT/INSERT/lock
   operations, sequence usage, RLS visibility, and denied UPDATE/DELETE/TRUNCATE
   operations. Run the Comdesk importer and fixed 30-column exporter against a
   disposable development fixture, then roll back the fixture transaction.
3. Confirm legacy Comdesk import/export also works with the switch off and with
   the identity tables absent. Compare clinic IDs, existing UUIDs, and medical
   institution codes before and after; they must remain unchanged.
4. Keep production migrations unapplied until the development checks and
   reviewed backfill preview pass. Then apply the three migrations in order in
   the approved production window, still with the feature switch off.
5. After a production read-only preflight and runtime permission check, enable
   the switch for a controlled canary. Disable it by setting it back to `0` if
   identity lookups fail; this does not undo or rewrite stored identities.
6. Handle candidate backfill as a separate reviewed operation. Do not use UUID
   alone to force a link; unresolved conflicts remain in Human Review.

Cloud Run code paths are not changed by these migrations. Do not enable the
switch or update Cloud Run configuration until the separate Cloud Run cutover
review approves its environment settings.

## Development verification snapshot

Database: `clinic-lead-shadow` (development only). The initial identity
migration, ACL hardening migration, runtime access migration, and audit conflict
policy correction were applied there. The application switch remained off.

The effective `clinic_runtime` role successfully completed a rollback-only SQL
smoke test covering the importer's entity/link/audit/UUID inserts, the mapping
row lock, and the export join. The same transaction confirmed that UUID UPDATE,
row UPDATE, DELETE, and TRUNCATE are rejected. Test rows were rolled back; no
clinic row, clinic ID, existing UUID, or medical code was changed. A temporary
`SET ROLE` grant for the SQL admin was removed immediately after the check.

READ ONLY candidate counts from this development snapshot:

| Check | Rows |
| --- | ---: |
| Unlinked Comdesk originals | 2,809 |
| Duplicate source-hash/row-number pairs | 0 |
| Unlinked originals with matching `source_records` row | 2,809 |
| Matching source rows already carrying a clinic ID | 0 |
| Unique active-clinic UUID candidates | 1,323 |
| UUIDs shared by multiple active clinics | 0 |
| Exact name/address/phone candidates | 0 |
| Additional normalized name/address candidates | 18 |
| Automatic links safe under the current checks | 0 |
| Human Review candidates | 1,341 |
| Candidates with no match | 1,468 |

The 1,323 UUID candidates all have at least one normalized information
conflict. Their conflict patterns are 4 name-only, 979 address-only, 339
name-and-address, and 1 name-and-phone. The 18 normalized name/address matches
are separate from UUID candidates. None of these rows should be linked solely
because a UUID or approximate name/address matches.

Unit tests cover same-entity UUID reuse across two project exports and preserve
the established 30-column Comdesk CSV contract. Those tests do not populate the
database's `sales_entity_project_memberships` table; project deduplication is
currently performed from the caller's project key and common-clinic ID.
