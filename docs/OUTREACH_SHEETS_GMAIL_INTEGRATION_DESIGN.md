# Clinic Lead Sheets and Gmail integration design (READ ONLY proposal)

This document records the source audit and a non-production implementation proposal. No database migration, Sheets write, Gmail send/schedule, GAS deployment, or Cloud Run deployment was performed.

## Draft-save and I3 send-preparation gates

The source repo and audited Sheet headers contain no authoritative definition of “I3”. Do not infer a meaning for I3. Draft-save preparation is a separate gate: the operator confirms the exact clinic/recipient, template, article and clinic detail, and current Gmail/ledger duplicate state must be `CLEAR`; the backend independently repeats missing-data, history and duplicate checks. A draft-save record never represents send approval. After a Gmail draft exists, a separate IS-lead send-readiness gate requires an approved I3 definition ID/HTTPS reference, explicit acknowledgement, exact content/identity/article/duplicate checklist and reviewer/time. Without the formal I3 definition, this second gate remains unapproved. There is no Clinic Lead send or schedule operation.

## Source audit

Source: [第一営業部_メール日程調整管理](https://docs.google.com/spreadsheets/d/1fqzCokKT2q5Iv4rMZBg8oSSXmtj2x_vdnw7sJqTbT9s/edit). Read through connected Drive tools; values containing personal or contact information were not emitted into the audit output.

| Tab | Non-empty data rows | Primary key / shape |
|---|---:|---|
| シート1 | 0 | empty |
| 設定 | 24 | key/value/config rows |
| フォーム回答 | 177 | 案件ID; timestamp and JSON payload |
| 案件管理 | 259 | 258 nonblank unique 案件ID; 83 columns |
| 集計 | 13 | KPI/aggregate rows |
| 日程候補ログ | 885 | 案件ID + 候補番号; repeated case IDs are expected |
| メール管理 | 272 | 案件ID; message state snapshots |
| トラッキングログ | 1,460 | event rows; 162 distinct case/token identifiers |
| 案件管理_整理前_20260911_135622 | 8 | archived case history; 77 columns |
| v3下書き管理 | 67 | Gmail Draft ID; UUIDs may be absent |
| 記事掲載_Gmail照合_20261016 | 328 | 管理ID / clinic_id / Clinic UUID; one reconciliation snapshot |
| 328医院_下書き送信予約照合_20261016 | 328 | 管理ID / clinic_id; draft/sent/scheduled snapshot |
| ClinicLead_GAS連携 | 0 | integration contract headers only; operation_key is intended idempotency key |
| 328医院_現在進捗_20261018 | 328 | clinic_id; another progress snapshot |
| Gmail統合管理_20261019 | 59 | Gmail/case reconciliation rows |
| 重複確認_20261019 | 4 | duplicate review rows; do not delete or merge automatically |
| Gmail現況照合_20261019 | 21 | current Gmail reconciliation snapshot |

There are 4,233 non-empty data rows summed across tabs. This includes configuration, KPI, event and overlapping snapshot/history rows; it is not a unique case or clinic count. The `シート1` and `ClinicLead_GAS連携` data sections are empty. Date columns include date-time formatted values and mixed text values; identifiers must remain strings during raw import (even numeric-looking IDs), and JSON form payloads must be retained verbatim. Keep each source row and tab independently, including duplicates and historical snapshots.

## Import and matching

The first full import creates a source batch per spreadsheet revision/tab (or per CSV checksum), then immutable raw rows with source tab, original row number, ordered raw headers, original cell strings, hash, and import time. Only after preview and human approval should rows be mapped into clinic/case/message/event records. Match in this order using explicit values: Clinic UUID, Clinic ID, source case ID, and operation_key. Each is a distinct key; none authorizes collapsing multiple source rows or cases. Ambiguous/mismatched values are retained as conflicts for review.

Subsequent syncs use source revision/cursor and stable source event keys. They append history and update only when the source is authoritative and the current value has not diverged. A divergent value is logged as a conflict and is not overwritten. Proposed authorities:

| Data | Authority |
|---|---|
| Gmail draft/sent/scheduled/reply state | Gmail API; Sheets is a reconciliation source |
| IS/FS assignment and form input | existing Google Form/GAS/Sheets |
| Article selection | confirmed ArticleAudit result |
| New open/click events | new tracking API; old tracking rows remain provenance |
| Appointment state | Google Calendar plus existing GAS history, with conflicts reviewed |

Keep the Google Form → existing GAS → Sheets path unchanged. Full import is a separately reviewed one-time snapshot; incremental sync is a later resumable operation with cursor and per-row errors. Neither is enabled by the local UI preview.

## Proposed normalized tables

The migration file is an unapplied proposal and assumes the existing delivery-ledger migration and a separately provisioned restricted database role. Tables and key controls:

| Table | Purpose / integrity |
|---|---|
| `outreach.sales_cases` | One row per operation/case; unique `operation_key`, optional unique source-system/external-case ID, clinic FK, allows multiple cases per clinic |
| `outreach.email_delivery_records` | Existing delivery ledger linked to sales case; retains existing `(clinic_id,campaign_id)` and idempotency uniqueness; non-unique case-history index allows the ledger to represent multiple historical delivery rows per case where permitted |
| `outreach.email_draft_save_reviews` | Append-only confirmation for the operator’s pre-draft review; does not assert I3 or send approval |
| `outreach.email_pre_send_reviews` | Append-only IS-lead review after a draft exists, with timestamp, I3 reference and checklist snapshots |
| `outreach.gmail_messages` | Gmail IDs/status and immutable recipient/content snapshots; unique operation, draft and message IDs |
| `outreach.email_state_history` | Append-only status transitions; source event key unique when provided |
| `outreach.article_selection_results` | Versioned ArticleAudit results with supersedes pointer |
| `outreach.engagement_events` | Open/click/reply; unique `(source_system, source_event_key)`; privacy-minimized metadata |
| `outreach.calendar_appointments` | Calendar/GAS appointment history; external event uniqueness by provider |
| `outreach.source_import_batches`, `source_import_rows` | Revision/checksum/tab/row provenance and verbatim raw columns/values; unique batch/tab/row |
| `outreach.sync_jobs`, `sync_errors` | Idempotent sync operation, cursors, counts and retryable row failures |
| `outreach.audit_events` | Actor/action/entity hashes, append-only |

Every new table enables and forces RLS, revokes public/anon/authenticated/runtime access, and grants only the restricted outreach role. Append-only evidence tables revoke UPDATE/DELETE/TRUNCATE; mutable state tables grant column-scoped updates. The existing PR #112 `clinic_draft_guard` remains the atomic, Clinic-ID-wide draft reservation guard; this integration does not remove it. Gmail historical records may be retained per observed Draft/Message ID, while new drafting still follows the existing guard. The role itself is not created by this proposal. Supabase documents that RLS and SQL privileges work together; production deployment still requires reviewed policies, role grants and connection verification ([Supabase RLS documentation](https://supabase.com/docs/guides/database/postgres/row-level-security)). Migration is not applied.

## Local UI and boundary

The existing “メール送付文章” UI is reused. It adds an exact body/identity/article/clinic-page review, explicit duplicate status and reviewer/time, Gmail draft-only action, status/history view and raw CSV preview. The strict 328-row legacy preview is reused when canonical headers are present. There is no send or scheduled-send path. Sheets/CSV previews preserve original values and never write or deduplicate. Gmail draft creation remains disabled unless explicitly configured and the dedicated database role is available.

