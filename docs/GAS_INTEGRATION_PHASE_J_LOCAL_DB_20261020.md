# Phase J: Local outreach DB ingestion and UI integration

## Safety boundary

The only database used was `phase_j_outreach_import_v1`, in `/private/tmp/clinic_phase_j_pgdata_20261020`, served on a private Unix socket under `/private/tmp/clinic_phase_j_socket_20261020`. The importer verifies the exact database name, exact PostgreSQL data directory, and absence of a TCP server address before every import or UI read. Production Supabase and the Shadow project were not connected during writes. Gmail and Sheets remained READ ONLY; no GAS, Cloud Run, or existing UI process was stopped or restarted.

The local fixture database was initialized from scratch with minimal `public.clinics`, `outreach.email_contact_events`, and its summary view, then the existing outreach, bridge, shared-draft, and TRASH constraint proposals were applied locally. The database has 22 `outreach` base tables. Migration execution was limited to this new local cluster.

## Snapshot and import semantics

All **4,298 rows across 17 tabs** were saved in `outreach.source_import_rows`, with ordered `raw_headers` and `raw_values`, source tab, source row number, SHA-256 row hash, Clinic ID/UUID candidates, GAS case ID, mapping status/reason, and the Sheets acquisition timestamp (`2026-10-20T12:26:53.139Z`). Each tab has a deterministic batch tied to the workbook checksum and retrieval timestamp. A rerun can promote an unresolved row to MATCHED if evidence improves, but preserves already MATCHED/APPROVED mappings and every original row.

| Raw-row mapping result | Rows |
| --- | ---: |
| MATCHED | 681 |
| HOLD | 3,617 |
| Total | 4,298 |

UUID linkage used the 226 explicit ID/UUID pairs in the workbook audit tab plus eight new Clinic Master matches from Phase I. Case linkage via progress-row pointer additionally required exact clinic name and email agreement. The eight live Clinic Master matches required exact normalized phone and exact clinic name. There were 74 Clinic/GAS case identity links in the local fixture; 31 of those cases had Gmail state UNKNOWN and were not inserted into `sales_cases`.

**43 identity-linked sales case rows** were inserted and are keyed separately by `clinic_uuid` / `clinic_id` and `external_case_id` (GAS案件ID). The source has no explicit campaign ID, so it is stored as blank and these 43 rows remain `ON_HOLD` with the reason `Campaign IDは原票にありません`; **0 are operational OPEN**. No campaign identifier was inferred. Unknown Gmail states and UUID/case ambiguities stay in source-import HOLD rows.

Additional exact-ID or exact-Thread linked history:

| Local logical table | Rows | Meaning |
| --- | ---: | --- |
| `gmail_messages` | 56 | Exact Gmail ID/Thread ID state matches linked to an identity-confirmed case |
| `email_state_history` | 56 | Append-only state observations with deterministic source event keys |
| `article_selection_results` | 7 | Source article URLs; status remains UNCONFIRMED unless explicit |
| `engagement_events` | 58 | Source tracking events; click/open confidence is preserved |
| `calendar_appointments` | 35 | Candidate times are PROPOSED; no Calendar or Meet event was created |

No message was linked by recipient-only similarity. The imported Gmail lifecycle vocabulary uses `TRASH` and `CANCELLED` as distinct values. No `CANCELLED` or actual Calendar/Meet event was inferred from source absence. Gmail acquisition time is stored separately from the Sheets snapshot time on `gmail_messages.last_synced_at`.

## Rerun and schema validation

After initial import, the importer was run three more times against the same workbook checksum. The final two repeat runs inserted **0** source rows and **0** business rows. Counts remained at 4,298 source rows, 43 sales cases, 56 Gmail messages, 56 state events, 7 articles, 58 engagement events, 35 appointments, and 3,617 HOLD source rows. A one-time reclassification promoted 323 previously unresolved rows after the full 226-pair reference map was seeded; it did not add rows.

The 22 local outreach base tables have RLS enabled and forced by the schema setup; runtime reads through the UI use the restricted `clinic_outreach_runtime` role and a read-only transaction. Tests verify runtime SELECT, denial of state-history UPDATE, and acceptance of distinct `TRASH` and `CANCELLED` values in a transaction that is rolled back.

## UI and tests

The existing local viewer now includes a Phase J panel reading only this isolated database. It shows Clinic/GAS-linked case rows, UUID, Gmail status and IDs, reply/state history, article URL, click/open counts, appointments and Meet URL, case HOLD reason, and last sync time. Unlinked raw rows display their tab, original row number, mapping status, reason, and snapshot timestamp. No data is synthesized for absent fields.

`AppTest` rendered 10 dataframes with zero exceptions and showed 43 locally linked cases, 0 OPEN, 43 ON_HOLD, and 4,298 source rows. It did not connect to, restart, or stop the existing 8513/8514 process. The read-only local DB UI query itself is exercised by the isolated database test.

## Remaining before Supabase Production

1. Verify all migration DDL, SECURITY DEFINER ownership/search paths, grants, RLS policies, and function privileges against an approved isolated clone of the actual Supabase schema.
2. Resolve the missing campaign IDs for all 43 locally linked cases before changing their status from ON_HOLD.
3. Review the 3,617 source HOLD rows, including UUID unknowns, Gmail unknowns, ID contradictions, tracking rows without a unique case link, and unsupported source tabs.
4. Confirm all 56 Gmail links remain unique by exact Gmail ID/Thread ID and reconcile any API-state drift. Never create a replacement Draft during review.
5. Approve a separate import plan and Production change window. No Production database has been written and no migration was applied outside the disposable local cluster.
