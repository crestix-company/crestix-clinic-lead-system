# Phase G: Local READ ONLY Sheets/Gmail integration

## Scope and controls

- Retrieved the native workbook `第一営業部_メール日程調整管理` through the connected Google Drive reader and exported a local XLSX snapshot. The source workbook was not edited.
- Read Gmail list/search metadata from the connected `info@crestix-inc.com` account. No full message body retrieval, draft creation/update, send, schedule, label change, or trash operation was performed.
- No database connection, migration, GAS/Sheets write, deployment, or external service mutation was performed.
- The local OAuth token path is not configured in this worktree. The app reads the connector-produced local snapshot and does not refresh Google credentials itself.
- Snapshot files live outside the repository in `/private/tmp`, with mode `0600`; no source row or mailbox metadata was committed.

## Sheets snapshot

The workbook contains 17 tabs. All original data rows and source tab/row coordinates are retained in the local viewer. The 4,297 non-empty data rows are distributed as follows:

| Tab | Rows |
| --- | ---: |
| 案件管理 | 259 |
| メール管理 | 278 |
| 328医院_現在進捗_20261018 | 328 |
| 日程候補ログ | 915 |
| トラッキングログ | 1,482 |
| Other populated tabs | 1,035 |

The other populated tabs include form responses, configuration, historical cases, draft reconciliation, article matching, Gmail reconciliation, duplicate review, and aggregation. The empty `シート1` and `ClinicLead_GAS連携` data areas remain empty in this snapshot.

## Gmail read-only reconciliation

Completed mailbox searches returned 4 live drafts, 23 Scheduled messages, 564 Sent messages since 2026-08-31, 38 Trash messages, and 405 inbound messages since 2026-08-31. Sent/received searches were paged to completion. Scheduled and Trash searches covered their current Gmail folders.

Rows are confirmed only when a Draft ID, Message ID, or Thread ID matches. Recipient-only resemblance does not assign a state. A missing Draft ID remains `UNKNOWN` unless one of Scheduled, Sent, Trash, or Replied has an exact identifier match.

| View | Rows | Gmail/Sheets exact match | REVIEW |
| --- | ---: | ---: | ---: |
| 案件管理 | 259 | 133 | 126 |
| メール管理 | 278 | 148 | 130 |

For 案件管理, exact Gmail states are SENT 154, REPLIED 31, SCHEDULED 1, TRASH 2, and UNKNOWN 71. The mail-management tab also has 3 exact current DRAFT matches; these are not linked to a current 案件管理 row by GAS case ID. The UI preserves them in the full mail-management table and does not create replacements.

## ID mapping and import preview

The article/Gmail audit tab provides 226 unique Clinic ID-to-UUID mappings, with no conflicting mapping in that tab. The case list can resolve 66 of 259 Clinic UUIDs; 193 remain `未取得` and display a HOLD reason rather than an inferred identifier. Article URLs are present on 158 case rows; 101 remain `未取得`.

The full-workbook no-write preview classified 4,297 source rows as:

| INSERT candidates | UPDATE | NOOP | HOLD |
| ---: | ---: | ---: | ---: |
| 787 | 0 | 0 | 3,510 |

The preview has no connected local or Supabase target ledger, so UPDATE/NOOP cannot be established and stay zero. INSERT means a row has a unique, explicit mapping in the read-only audit data; it is only a candidate and was not persisted. Unmapped, ambiguous, duplicate, configuration, and non-clinic rows remain HOLD. Repeated clinic histories are retained as distinct source rows.

The existing `重複確認_20261019` tab has 4 data rows; they remain visible with their original row numbers and are not auto-resolved because clinic identity is not uniquely available in those rows. Repeated 案件ID values in 日程候補ログ and トラッキングログ represent multiple candidate/event history rows and are retained, not collapsed.

The UI also accepts multiple UTF-8/UTF-8-BOM/CP932 CSV files and retains tab/file name, original fields, logical source line, and every row. Browser upload of a one-row synthetic CSV added one HOLD row, and the screen showed INSERT 787 / UPDATE 0 / NOOP 0 / HOLD 3,511. It presents counts without writing to any database or sheet.

## Local UI and verification

- Start with `python3 -m streamlit run scripts/phase_g_local_ui.py --server.address 127.0.0.1 --server.port 8514 --server.headless true`.
- Local URL: `http://127.0.0.1:8514`.
- The existing Phase F mock UI on port 8513 was left running and unchanged. The local HP research port 8510 and other worktrees were not accessed.
- `AppTest` loaded 259 case rows, all 278 mail-management rows, and 259 raw case rows without Streamlit exceptions. A search using an actual clinic name narrowed 259 cases to one result.
- Chrome loopback smoke on port 8514 verified the page, the case and mail tables, CSV upload, and the workbook import preview. Screenshots are `/private/tmp/phase_g_local_ui.png` and `/private/tmp/phase_g_local_ui_cases.png`.
- Targeted pytest: 46 passed across Phase G snapshot/reconciliation, Phase F mock adapter, and Gmail workflow/read-only isolation tests.

## Remaining before an always-refreshing local deployment

1. Configure and approve a local read-only Sheets credential for direct refresh; no local Sheets OAuth credential currently exists.
2. Configure the existing Gmail OAuth reader with `gmail.readonly` for refresh inside the app. Current rows came from the connected read-only Gmail tool; the application does not contain that connector.
3. Resolve 193 case UUIDs through an authorized Clinic Master read-only lookup, and confirm 3 live Draft records whose case IDs are absent from 案件管理.
4. Read a local/isolated target ledger to classify UPDATE and NOOP accurately. Keep Production Supabase disconnected until separately approved.
