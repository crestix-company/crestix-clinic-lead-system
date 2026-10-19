# Comdesk CRM snapshot diff import

## Input contract and current validation boundary

The importer accepts the sales/progress export and the clinic/UUID export as a
pair. It joins only by non-empty Comdesk UUID, requires one row per UUID in each
file, then checks clinic name, address, and phone against both the other export
and the already-approved clinic/UUID mapping. Two identity fields must agree;
any populated-field conflict or ambiguous mapping goes to HOLD. It never
generates a UUID or changes `clinic_id` / `common_clinic_id`.

`データ_20261019182717.csv` and `エクスポート_20261019182722.csv` were described as
30-column files with 1,411 matching UUIDs, but the actual files were not present
in this checkout. Therefore exact source header names, encoding, blank-value
conventions, and the 1,411-row reconciliation have not been validated here.
`read_snapshot_csv` requires an explicit map from observed headers and rejects
files that do not have exactly 30 columns; no guessed map is shipped. The
existing Comdesk 30-column import/export path is unchanged.

Before production integration, add a strictly anonymized fixture made from the
two originals after confirming the exact header map and encoding. Do not add
the originals, patient/clinic data, UUIDs, or staff names to Git or a PR.

## Snapshot and history semantics

Sales progress, last-call date/agent, call count, and newline-separated project
memberships are stored as the latest clinic snapshot. `履歴` is treated as a
free-text snapshot. The first non-empty value is recorded as an initial text
snapshot; subsequent changed text creates a before/after history-change row.
These rows are not call events. Identical history creates no history row.
Identical CSV pairs are skipped using both file SHA-256 values. A later pair
with unchanged content only advances snapshot observation metadata. Older or
ambiguous same-timestamp snapshots are held.

The additive migration creates batch, current snapshot, history-change, and
HOLD tables. RLS is enabled and broad roles are revoked; `service_role` gets
only SELECT/INSERT or SELECT/INSERT/UPDATE as needed, with no DELETE/TRUNCATE.
It does not modify the clinic identity tables. Migration has not been applied
to any database in this task.

## Stable call-event identifiers

The two supplied export descriptions identify only a per-clinic `履歴` text
field, not a per-call key or update timestamp. Public Comdesk Lead pages confirm
that an API is available for call/activity history integration, but do not
publish endpoint fields or guarantee a stable call ID in the pages reviewed.
Request the tenant-specific API schema or a dedicated call-history export from
Comdesk and confirm a stable event ID plus update timestamp before adding true
call-event synchronization. Until then, this feature stores text snapshots only.

## Validation

`tests/test_comdesk_snapshot_diff.py` uses synthetic, anonymized rows to cover
UUID pairing, identity mismatch, multi-project memberships, first/identical/
changed imports, UUID-less HOLD, stale timestamps, and explicit 30-column
header mapping. These tests validate planning behavior, not a live PostgreSQL
transaction or the unavailable original CSVs.
