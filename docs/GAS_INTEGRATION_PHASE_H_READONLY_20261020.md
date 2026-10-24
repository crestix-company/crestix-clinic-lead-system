# Phase H: clinic identity reconciliation and Supabase import preparation

## Scope and source freshness

- This phase reuses the Phase G local snapshots: the 17-tab workbook export and the read-only Gmail metadata export for `info@crestix-inc.com`.
- The snapshots were retrieved on 2026-10-20 (Sheets about 20:48 JST, Gmail about 20:51 JST). The UI labels these as acquired snapshots and explicitly says that no latest API reconciliation ran in Phase H.
- No new Sheets/Gmail request, database connection, write, migration, OAuth issuance, or production operation was performed in this phase.
- The connected Clinic Master adapter requires `CLINIC_READONLY_DB_URL`, verifies SELECT-only privileges, and sets the transaction read-only. That variable is absent in this worktree, so no Clinic Master lookup was attempted. UUID enrichment from Clinic Master in this phase: **0**.

## REVIEW classification

The REVIEW table keeps each source row and lists all applicable reason labels. Reason counts may overlap and must not be summed. Tab totals are independent; the cross-tab union uses GAS case ID and is shown separately. A clinic name alone is never used as a joining key.

| Source | REVIEW rows | Draft ID changed/missing | Draft moved to Scheduled/Sent or later state | Case ID discrepancy | Name/email discrepancy | Multiple cases per clinic | UUID missing | No counterpart |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 案件管理 | 126 | 70 | 46 | 0 | 0 | 0 | 126 | 1 |
| メール管理 | 130 | 71 | 47 | 0 | 0 | 0 | 130 | 5 |

There are **130 distinct REVIEW case IDs** across both tabs and **one REVIEW row without a case ID**; overlapping case IDs are not counted twice. No REVIEW row was automatically cleared. Name/email disagreement and multi-case reasons are zero with the available exact-ID comparisons, which does not prove the names are correct. UUID values absent in the source rows are not filled by name-only matching. `案件ID不一致` is assigned only when a different-ID counterpart is found by exact UUID, Clinic ID, or email evidence; otherwise the row is `対応先なし`.

The Draft ID reason means an expected Draft is missing from the acquired Draft list and no exact matching message/thread evidence resolved it. The move reason is limited to exact identifier matches where a Draft-like sheet status now has a later Gmail state. These cases remain review candidates; no Draft was recreated.

## UUID resolution

- The existing article/Gmail audit tab has 226 unambiguous Clinic ID-to-UUID pairs; the current 案件管理 view has 66 UUIDs already resolvable from explicit source mappings, leaving the reported 193 UUID-missing rows.
- The authorized read-only Clinic Master route is fail-closed and unavailable because its dedicated URL is not configured. No additional UUID was assigned.
- Before a lookup can run, set a dedicated SELECT-only URL, confirm the role privileges on every referenced table, and run the repository’s `verify_read_only_role` check. Confirm candidates by immutable Clinic ID/UUID plus address or phone evidence. Never overwrite an existing confirmed UUID; collisions go to manual review.

## Supabase preview and schema comparison

No database target is connected, so no source row is reported as a confirmed INSERT, UPDATE, or NOOP. The local preview reports **0 importable**, **767 unmatched candidates**, **3,357 unmatched**, and **173 HOLD** rows. “Candidate” means explicit mapping evidence is present but target-row comparison and operator approval are still missing. Rows with a Gmail identifier but no exact Draft/Scheduled/Sent/Trash/Reply evidence are HOLD. Every preview row preserves source tab, original row number, raw values, and logical table.

Logical mapping against the existing additive Phase G schema (`20261019160000_outreach_full_sheets_gmail_integration.sql`):

| Phase H source purpose | Existing target proposal |
| --- | --- |
| Original snapshots and raw rows | `source_import_batches`, `source_import_rows` |
| Sales cases and campaign ownership | `sales_cases` |
| Draft/message records | `gmail_messages` |
| State transitions | `email_state_history` |
| Article decisions and URLs | `article_selection_results` |
| Click/open tracking estimates | `engagement_events` (`confidence` must retain inference/unknown provenance) |
| Appointment and Calendar/Meet identifiers | `calendar_appointments` |
| Import/sync executions and failures | `sync_jobs`, `sync_errors`; `audit_events` for reviewed actions |

These proposals are additive to existing `outreach.email_delivery_records`; they do not replace historic rows. Multiple cases for a clinic are permitted by `sales_cases`. Gmail messages and state history retain distinct operation/message/event IDs. The Phase B draft lease migration separately defines atomic operation reservations and fencing tokens; it is a proposal only.

Static migration review confirms explicit table constraints, partial unique indexes for nonempty external IDs, RLS enable/force on proposed tables, revokes from public/anon/authenticated and `clinic_runtime`, and narrow `clinic_outreach_runtime` grants. The Phase B functions are `SECURITY DEFINER` with empty search paths, revoked from public/anon/authenticated, and executable by the dedicated runtime role. Before any application: test on an isolated clone; verify all SECURITY DEFINER function ownership/body and schema-qualified references; verify grants and RLS behavior as the actual runtime role; check migration ordering, role existence, uniqueness collisions against current data, and rollback/recovery. This static review is not a database application or production verification.

One status-model gap remains: `outreach.gmail_messages.message_status` currently allows `CANCELLED` but not `TRASH`, while this integration must preserve Gmail Trash separately from cancellation. Do not map Trash to Cancelled. Add and review a forward-only status constraint migration (and matching model validation) before any import; leave `TRASH` distinct in the preview meanwhile. The Phase B operation table uses `TRASHED`, so its API-to-history mapping must also be explicit.

## Local UI and safety

The Phase G viewer now includes a Phase H panel for per-tab REVIEW reasons, cross-tab case-ID union, UUID lookup availability, Gmail/Sheets comparison, snapshot time/source, and importable/unmatched/HOLD counters. It retains the raw source-row preview. Phase G CSV preview remains local-only; repeated previews are deterministic and do not mutate any data.

The local Gmail status values remain `DRAFT`, `SCHEDULED`, `SENT`, `REPLIED`, `TRASH`, or `UNKNOWN`. State is confirmed only by exact Draft/Message/Thread identifiers. If Gmail search data is absent or no exact match exists, state remains `UNKNOWN` and the row stays REVIEW/HOLD. No Gmail create/update/send/schedule operation exists in this Phase H path.

## Verification and outstanding items

- Phase H plus Phase G/F-adjacent focused tests: 52 passed under the isolated Python 3.12 environment.
- Full Python 3.12 pytest: 2,300 passed, 56 skipped.
- Streamlit AppTest: 0 exceptions; Phase G browser smoke on existing port 8514 rendered 9 dataframes and the CSV preview. Its legacy Phase G counters remain labeled INSERT/UPDATE/NOOP/HOLD for compatibility; the separate Phase H counters are 0 importable / 767 unmatched candidates / 3,357 unmatched / 173 HOLD. Ports 8513/8514 were not stopped.
- Before importing, obtain an approved Clinic Master read-only connection, current snapshots, exact case-ID/Gmail-state review decisions, a read-only target DB comparison, and authorized mapping approval. Only then can `INSERT/UPDATE/NOOP/HOLD` be finalized.
