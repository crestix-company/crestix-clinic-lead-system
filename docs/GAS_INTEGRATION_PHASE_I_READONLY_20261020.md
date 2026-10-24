# Phase I: Clinic Master reconciliation and sales-history import preparation

## Scope and safety

Phase I used the connected Clinic Lead Shadow Supabase project, Google Sheets connector, and Gmail READ ONLY search. All three reads were completed on 2026-10-20. No credential was issued or changed. No Gmail, Sheets, database, migration, GAS, Cloud Run, or Production write occurred. The only local changes are UI/planner code, tests, this document, and an unapplied migration proposal.

## Clinic Master match

The source population was the 193 案件管理 cases without a UUID in the Phase G snapshot. Clinic Master matching required an exact case-insensitive clinic-name match **and** exact digits-only normalized phone match, with exactly one active, non-merged, non-held clinic row. Name-only matching was not used. Existing explicit UUID values were not candidates for overwrite.

| Result | Count |
| --- | ---: |
| MATCHED | 8 |
| REVIEW | 185 |
| REVIEW: exact name + phone did not identify a unique row | 145 |
| REVIEW: source phone missing | 40 |

The result snapshot is an ephemeral local artifact outside Git, mode 0600. Its database snapshot time is `2026-10-20 12:25:03.502833+00 UTC`. The 193 source cases decreased to 185 UUID-missing cases. Within rows already marked Gmail/case REVIEW, UUID不足 reason counts changed from 126→123 in 案件管理 and 130→127 in メール管理; the other five matches were outside those REVIEW subsets. Overall Gmail/case REVIEW rows remain 126 and 130; identity enrichment did not clear a Gmail or campaign ambiguity.

## Latest READ ONLY snapshots

`GoogleSheetsReadOnlyAdapter.read_tabs` was used for the latest ranges and merged into the pre-existing 17-tab workbook snapshot. The five refreshed tabs contain 259 案件管理 rows, 278 メール管理 rows, 1,483 トラッキングログ rows, 915 日程候補ログ rows, and 328 progress rows. The full workbook contains 4,298 rows. Sheets read time: `2026-10-20T12:26:53.139Z`.

Gmail query completion time: `2026-10-20T12:29:13.056Z`. Counts are 4 current Drafts, 23 Scheduled, 616 Sent (since 2026-08-31), 38 Trash, and 135 inbound messages whose exact thread ID intersects an exact Sent thread. Replied is therefore a strict thread-linked subset, not the broader 463 inbound-message search pool reported in the raw manifest. Searches completed all pages. Only IDs, thread IDs, labels, timestamps, and query counts were retained; message bodies were not fetched. Missing Draft IDs are not treated as proof of deletion; all other lifecycle buckets are checked before deciding and unresolved identity remains UNKNOWN/HOLD.

The live set contains 57 Sent IDs absent from the older snapshot and lacks 5 older Sent IDs; Scheduled and Trash counts did not change. The strict Replied definition differs from Phase G's broad inbound pool, so its lower count is a method change, not evidence that replies disappeared.

## Shadow database comparison and import preview

The connected database was queried read-only. In `outreach`, the only base table is `email_contact_events` (0 rows) and `clinic_email_summary` is a view. The proposed targets `sales_cases`, `gmail_messages`, `email_state_history`, `article_selection_results`, `engagement_events`, `calendar_appointments`, `source_import_rows`, and `sync_jobs` do not exist in this project. Consequently there are no comparable existing rows for the proposed import model.

The Phase I preview retains all 4,298 source rows and source values, source tab/row, case ID, mapped Clinic ID/UUID when available, Gmail status, and proposed logical/target table. Since the target relation does not exist, the action totals are **INSERT 0 / UPDATE 0 / NOOP 0 / HOLD 4,298**. These are not database writes. Distinct cases, message rows, and events are not deduplicated or merged.

## Migration proposal: TRASH status

`20261019160000_outreach_full_sheets_gmail_integration.sql` currently permits `CANCELLED` but not Gmail `TRASH`. The unapplied follow-up `20261020130000_gmail_message_trash_status.sql` replaces only that status CHECK set and allows `TRASH` and `CANCELLED` as separate values, while preserving DRAFT/SCHEDULED/SENT/REPLIED/UNKNOWN/CONFLICT. `email_state_history.next_status` remains append-only text and continues to retain each transition. The Python `canonical_gmail_message_status` adapter converts API/legacy `TRASHED` to storage `TRASH`; it never maps it to `CANCELLED`.

The migration was not applied. Before Production use, compare actual constraint names and rows, check state-value inventory, verify all role grants/RLS and migration ordering on an isolated clone, then obtain explicit Production approval.

## Local UI and verification

The existing Phase G viewer selects the Phase I snapshot automatically when present, shows Sheets/Gmail/Clinic Master retrieval times, per-case UUID match result/reason, REVIEW classifications, status counts, and logical target relation. The source workbooks and Connector snapshots stay read-only. `AppTest` displayed 17 tabs, 259 cases, 278 mail rows, 8 MATCHED / 185 REVIEW, and 0/0/0/4,298 import actions without exceptions. An HTTP GET to the existing loopback URL `http://127.0.0.1:8514/` returned 200. The browser CDP smoke endpoint at 9223 was unavailable, so a click-through browser session could not be performed in this run. No existing service was stopped.

## Remaining before Supabase persistence

1. Apply and validate both shared-draft and outreach proposals only in an approved isolated database; current Shadow lacks the destination tables.
2. Confirm migration order, status CHECK/data compatibility, RLS behavior under actual runtime roles, and append-only event grants.
3. Approve the eight Clinic Master matches and manually resolve 185 UUID cases; keep any conflicting IDs in HOLD.
4. Resolve the 126/130 Gmail and source-row reviews against campaign-level identifiers. Do not recreate missing Drafts.
5. Re-run target comparison against an approved database snapshot and separately approve any import. Production writes remain prohibited.
