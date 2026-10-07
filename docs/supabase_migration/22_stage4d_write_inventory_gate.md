# Stage4-D WRITE Inventory and Gate 1

Date: 2026-10-07  
Branch: `feature/supabase-shadow-migration`  
Starting HEAD: `214b48d` (`fix: place pg_trgm in extensions schema`)

## Safety baseline

- Current branch and expected recovery point were confirmed before inspection.
- Existing untracked Stage2/3 artifacts were left untouched.
- Production SQLite SHA-256 values matched the supplied baseline:
  - clinics: `fc53c9de851567c62f75a18aceb9521cb94b82385e69c19f09e880df386cc8a0`
  - Treatment: `36dbe570d19af61ca45c8571338a2cb3e3c28e472b42113286e0a0e093add804`
  - HP: `3980865ebd0de6f30dcebad98b1aada92252b00b3e7836874b408ffa1dbf7d53`
- Session Pooler READ ONLY health check: `SELECT 1` succeeded; clinics = 162,258; 19 mapped tables total = 632,903; `pg_is_in_recovery() = false`; `default_transaction_read_only = off`.
- No PostgreSQL or SQLite mutation was performed by this inventory.

## Runtime WRITE inventory

All table names below are SQLite source names; target names follow `scripts/supabase_migration/full_shadow_import.py`.

| Class | Runtime path / file | Tables and operations | Identity, transaction, side effects / status |
|---|---|---|---|
| A | `ClinicStore.import_comdesk` / `src/master/store.py`; UI `app_v2.py` | `provenance.templates` INSERT OR IGNORE; `public.clinics` INSERT/UPDATE; `provenance.source_records` INSERT/UPDATE; `provenance.comdesk_original_rows` INSERT; `provenance.match_reviews` INSERT/UPDATE; `provenance.change_history` INSERT; `provenance.import_batches` INSERT | `BEGIN IMMEDIATE`; batch/template/source hashes are deterministic text keys; source/original/review IDs use SQLite `lastrowid`; clinic IDs are allocated by SQLite for NEW matches. `_upsert` may update UUID/base data and `_project` recalculates normalized columns including `medical_key`. Not safe to move as-is under the no-ID/no-medical_key-regeneration constraints. |
| A | `ClinicStore.import_master` / `src/master/store.py`; UI `app_v2.py` | Same source/provenance tables as Comdesk plus `public.clinics.is_new` scoped UPDATE | `BEGIN IMMEDIATE`; import batch digest deduplicates; rejects older source dates; NEW clinics use SQLite `lastrowid`; `_project` derives `medical_key`. |
| A | `ClinicStore.save_research` / `src/master/store.py`; UI and `jobs._research_one` | `research.research_results` INSERT OR REPLACE; optional `research.hp_pages` DELETE-all-for-clinic then INSERT OR REPLACE; `provenance.change_history` INSERT; `public.clinics` projection UPDATE | `BEGIN IMMEDIATE`; merge is previous JSON then incoming keys; JSON is compact sorted UTF-8 text in SQLite and JSONB in Supabase migration; page PK `(clinic_id,url)`; timestamps are UTC ISO-8601 microseconds. Projection rewrites `medical_key`. Whole operation is one SQLite transaction. |
| A | `ClinicStore.override` / `src/master/store.py`; UI `app_v2.py` | `provenance.manual_overrides` DELETE or INSERT OR REPLACE; `provenance.change_history` INSERT; `public.clinics` projection UPDATE | `BEGIN IMMEDIATE`; PK `(clinic_id,field)`; `value_json` compact JSON text; `None` deletes override but still writes history; projection recomputes `medical_key`. |
| A | `ClinicStore.resolve_review` / `src/master/store.py`; UI `app_v2.py` | `public.clinics` INSERT/UPDATE; `provenance.source_records`, `comdesk_original_rows`, `match_reviews` UPDATE; `provenance.change_history` INSERT; duplicate resolution may call `merge_clinics` | `BEGIN IMMEDIATE`; generated clinic ID uses SQLite `lastrowid`; matching/merge may change clinic identity relationships and UUID/base data; file lock prevents concurrent research. Not safe to move without a separately proven ID-preserving contract. |
| A | Research job lifecycle / `src/master/jobs.py`; UI `app_v2.py` | `research.research_jobs` INSERT/UPDATE; `research.research_job_items` INSERT/UPDATE; states transition among PENDING/RUNNING/DONE/CANCELLED; error/budget recovery updates notes/status | Job ID is `uuid.uuid4().hex`; items PK `(job_id,clinic_id)` and FKs to job/clinic; `BEGIN IMMEDIATE` claim/state transactions; worker calls `save_research`. Runtime writer lock and background threads participate. |
| A | `_create_maps_hp_job` / `app_v2.py` | Direct SQLite INSERT into `research_jobs` and `research_job_items` | `BEGIN IMMEDIATE`; direct SQL bypasses Repository; UUID job ID, clinic IDs preserved from query. Must be moved behind the write contract before any full runtime cutover. |
| A | `CachedSearch.search` / `src/enrichment/search_provider.py` | `research.research_jobs.search_count` UPDATE; `research.search_usage` INSERT; `research.search_cache` INSERT OR REPLACE | Reservation transaction occurs before network call; failed external search remains charged; cache write occurs after network call in a separate transaction; no silent retry in provider. Cross-transaction behavior is intentional and must be retained. |
| A | `ClinicStore.set_setting` / `src/master/store.py`, exposed by `SqliteSettingsRepository`; UI/settings use | `app_config.settings` INSERT OR REPLACE | key/value PK; value compact JSON text; allowlist is `monthly_limit`, `external_usage_reserve`, `filter_defaults`, `age_basis`; no history row. |
| A | `import_maps_results` / `src/master/google_maps.py`; UI `app_v2.py` | `provenance.google_maps_results` INSERT; `public.clinics.base_json` UPDATE; `provenance.change_history` INSERT when base changes; `provenance.import_batches` INSERT | `BEGIN IMMEDIATE`; batch hash deduplicates; original batch rows retained; confirmed website is not downgraded by weaker outcomes; projection recalculates `medical_key`. |
| B | `scripts/research_worker.py` Treatment background worker | Sidecar `clinic_progress`, `page_cache`, `clinic_treatment_research_final`, `clinic_research_status` INSERT/UPDATE/DELETE | Separate Treatment SQLite sidecar; target output tables are `treatment.clinic_research_status` and `treatment.clinic_treatment_research`. `clinic_progress` / `page_cache` are operational state outside the 19-table target and still need an explicit Stage4-D destination/retention contract. Since this is an active background writer, it is mandatory under the A+B Stage4-D rule, not deferred. |
| B | `src/master/hp_research_batch.py` | HP batch sidecar `hp_research_batch_results` INSERT ON CONFLICT UPDATE | Separate HP SQLite sidecar; update increments attempts and refreshes rank/research fields. Shadow target exists as `hp_research.clinic_hp_research`. Active batch writer is mandatory under A+B; source/retry/progress transaction behavior and scheduling must be retained. |
| B | `src/master/national_maps_queue.py`; `src/enrichment/kouseikyoku_source.py` | Local collection queue/cache and source-fetch state writes | Not writes to the 19-table clinic runtime schema; classify as background collection runtime pending usage/callsite confirmation. Under the A+B rule, if enabled in the production workflow these writes must be Repository-backed or moved to explicitly designed operational storage before Stage4-D PASS. |
| C | `ClinicStore.reintegrate_existing` / `src/master/reintegration.py` | Wide UPDATEs across clinics, source_records, comdesk rows, reviews, jobs/items; research results/overrides/pages INSERT; merge fields and tel_match_key recalculation | Admin/reintegration transaction; file lock; may reassign associations and recalculates `tel_match_key` across all clinics. Explicitly deferred; conflicts with the no-cleaning/no-mass-update rule. |
| C | `repair_reset_job_items(..., dry_run=False)` / `src/master/jobs.py` | `research_job_items` UPDATE | Maintenance-only repair. Default is dry-run and rollback; persistent mode requires explicit IDs. Defer. |
| C | `ClinicStore.__init__` / `src/master/store.py` | DDL, possible backup, schema upgrades, and for old schema a full `clinics.tel_match_key` UPDATE | Constructor may write merely by opening an old database. Never instantiate it against Production for a test. Existing schema is current; initialization behavior must be isolated from runtime WRITE routing. |
| C | `scripts/reproject_legacy_departments.py`, import/migration/reintegration/export helpers | Bulk/local UPDATE/INSERT, staging DB changes, or backup output | Admin/migration utilities, not ordinary UI runtime. Do not include in Stage4-D runtime cutover. |
| D | `scripts/verify_fixed_export.py`, `scripts/phase7b_pilot.py`, `scripts/phase7c_canary.py`, perf/sample utilities | INSERT/UPDATE in temporary or fixture DBs | Test/pilot-only; not application runtime and must not be redirected to Production/Supabase. |

## Stage4-D target and deferrals

Under the Gate 1.5 definition, **A and B are both mandatory** for Stage4-D PASS: every ordinary UI and enabled background runtime WRITE must go through the Supabase Repository when the Supabase selector is active, and must fail explicitly (never auto-fallback) on failure. C admin/maintenance and D migration/test-only tools may remain SQLite-backed and deferred. Locally materialized Sales Classification and scrape/cache checkpoints are not clinic source-of-truth writes, but their runtime status and destination still need to be explicitly declared; they cannot be silently treated as production-data WRITE paths.

## Gate 1 result: NO-GO pending contract work

Two verified blockers prevent a safe adapter/canary at this point:

1. `ClinicStore._project()` always derives and writes `clinics.medical_key`; `save_research`, `override`, imports, review resolution, and Google Maps import call it. The user's Stage4-D safety rule prohibits regenerating/changing `medical_key`. A dry-run cannot be certified until the target contract explicitly preserves the existing key and behavior is proven equivalent for every operation in scope.
2. Job runtime state is not fully behind the Store/Repository boundary: `_create_maps_hp_job` issues SQL directly in `app_v2.py`; `CachedSearch` and the job runner issue more direct SQLite writes. Adding a selector only to `ClinicStore` would not control these writes and would violate the no-silent-fallback / WRITE Primary requirement.

Additional high-risk contracts that remain unimplemented: SQLite-generated integer IDs/`lastrowid` for new clinics and provenance rows; SQLite `INSERT OR REPLACE` delete+insert semantics vs PostgreSQL `ON CONFLICT`; integer ID sequence preservation; the split transaction around external search; history and clinic projection atomicity; Supabase writes currently connect as the `postgres` role (`rolbypassrls=true`), so an application-scoped least-privilege database role/grant plan is required before runtime credentials are used for WRITE.

No code adapter, feature-flag change, dry-run against mutable data, rollback canary, failure injection, persistent canary, Security Advisor change, or WRITE cutover was performed. This is a fail-closed Gate 1 stop, not a Stage4-D implementation failure. Stage4-D remains SQLite WRITE Primary; Stage5 is NO-GO.

## Gate 1.5 — contract audit (READ ONLY)

### 1. `medical_key` current behavior and identity contract

`src/master/matching.py:medical_key(record)` takes `record["clinic_id"]`, trims it, keeps the last colon-delimited component, removes commas and hyphens, and returns `<prefecture>:<medical_type>:<code>` only when both prefecture and medical type are present; otherwise it returns the empty string.

| Classification | Current call path | Observed behavior |
|---|---|---|
| A — existing clinic re-projection | `_project()` from `save_research`, `override`, import match, review resolve, Google Maps import, and age refresh | Rebuilds data from base + research + overrides, calls `medical_key(data)`, and includes it in a wide clinics UPDATE. An immutable-preserving adapter must not include `medical_key` in UPDATE columns. |
| B — new clinic initial generation | `_upsert` NEW and `resolve_review` with no target | `clinics` INSERT omits `medical_key` (default empty), returns `lastrowid`, then invokes `_project()` which derives it. Comdesk mapping does not carry `clinic_id`, so new Comdesk clinic keys are normally blank. MHLW official records can carry the official identifier. |
| C — source/projection update | matched MHLW import, matched review, later research/override/Maps projection | MHLW `_upsert` can add official `clinic_id` to an existing clinic's base, after which `_project()` can change a blank key to nonblank. This is a real current behavior, not merely a theoretical normalization change. |
| D — maintenance/reintegration | `src/master/reintegration.py` matching/reintegration path | Recomputes `medical_key(record)` for identity matching and separately recomputes `tel_match_key`; bulk reintegration is deferred and must not run as a Gate 2/runtime operation. |

READ ONLY Production audit over 162,258 rows found 16 stored blank `medical_key` values; recomputing from `effective_json` matched the stored value for all 162,258 rows (0 mismatches). This proves current projection consistency for the snapshot, but does not resolve future blank-to-known transitions.

**Candidate contract** (no implementation):

- Existing clinic row: never UPDATE `medical_key`, UUID, or clinic ID. Recompute only as validation. If recomputed key differs from stored key, abort the entire mutation with a typed identity-contract error and a redacted metric/event; do not mutate or auto-repair.
- New clinic row: create the row only after classifying source. Generate the initial `medical_key` only from a validated official source record that supplies official clinic identifier, prefecture, and medical type. Comdesk-only rows start with empty key. UUID is accepted only from source input; never generated by the application. After INSERT, clinic ID, UUID, and key become immutable.
- Projection refresh must exclude all three identity columns from UPDATE. The validation helper must distinguish “new row initial identity” from “existing row validation” by explicit operation type, not by testing whether the stored string is blank.

The candidate is **not consistent with all current behavior**: matched official MHLW input can turn an existing blank key into a nonblank key. If strict existing-row immutability is adopted, such an import must fail atomically or its accepted behavior must be changed explicitly. It cannot both preserve current behavior and satisfy the candidate invariant. This unresolved business choice is a Gate 1.5 blocker.

### 2. `_project()` responsibility split (design only)

Proposed separation:

1. `derive_clinic_projection(base, research, manual, existing_uuid)` — pure transformation; emits derived clinic columns and a candidate key but performs no SQL.
2. `generate_medical_key_for_new_clinic(official_record)` — callable only before initial INSERT; validates source type and required official fields.
3. `validate_existing_medical_key(stored, candidate)` — equality-only; raises on mismatch, including blank-to-known; no repair/write.
4. `refresh_clinic_projection_preserving_identity(...)` — updates only non-identity projection fields; identity fields are absent from the UPDATE allowlist and guarded by tests.

Needed callsite changes: `_upsert` NEW branch must compute initial identity before INSERT; `_project` callers in `_upsert`, `save_research`, `override`, `resolve_review`, Maps import, and `refresh_age_model` must use projection refresh; `resolve_review` NEW must classify official vs Comdesk source; reintegration stays deferred and must not invoke runtime projection. No code was edited.

### 3. Direct SQLite runtime WRITE matrix

| Path | Direct SQLite boundary / mutation | Class / Stage4-D treatment |
|---|---|---|
| `src/master/store.py` `set_setting`, import methods, `save_research`, `override`, `resolve_review`, `refresh_age_model` | `store.connect()` and inline SQL; imports/research/override/review/Maps all write locally; age refresh calls `save_research` once per selected clinic and then writes `age_basis` | A; replace with explicit Repository commands, not generic SQL passthrough. |
| `src/master/jobs.py` job create/pause/reset/limit/claim/run/finish/error transitions | `store.connect()` inline SELECT/INSERT/UPDATE and `BEGIN IMMEDIATE`; worker calls out to researcher and later saves results | A; all job state changes and claims must use Repository. Use conditional UPDATE/RETURNING or row locks for atomic claim. |
| `app_v2.py:_create_maps_hp_job` | Inline SQLite `BEGIN IMMEDIATE`, selects clinic IDs, inserts job and items | A; move into the same job Repository contract (no app SQL). |
| `src/enrichment/search_provider.py:CachedSearch` | Inline SQLite transaction for cache/quota/search_count/search_usage; second transaction for cache upsert | A; Repository methods for read/reserve and cache store; keep provider I/O outside transactions. |
| `src/master/google_maps.py:import_maps_results` | `store.connect()` transaction with provenance, clinic projection, history and batch marker | A; one Repository transaction. |
| `src/master/research_sidecar.py` | All attached opens use SQLite URI `mode=ro`; SELECT only | No runtime WRITE. Research Worker is a separate B writer. |
| `scripts/research_worker.py` | Writes Treatment sidecar progress/cache and Treatment result/status | B; migrate result/status writes behind a background Repository and explicitly decide where operational progress/cache state lives. |
| `src/master/hp_research_batch.py` | Writes HP batch sidecar; production clinic DB opens read-only | B; move result upsert behind background Repository; preserve attempts increment and batch failure bookkeeping. |
| `src/master/sales_classification.py` | On source signature change, creates/replaces a rebuildable local SQLite cache used by reads | Derived cache, not clinic primary data. Explicitly exclude from WRITE Primary accounting only if cache writes remain offline/local and rebuildable; no Production clinic DB writes. |
| `src/master/national_maps_queue.py`, `src/enrichment/kouseikyoku_source.py` | Collection/checkpoint/cache writes outside the 19-table application schema | B only if enabled as ordinary production background runtime; otherwise C/D offline collection. Confirm deployment callsites before Stage4-D scope freeze. |
| `ClinicStore.__init__` | `mkdir`, `PRAGMA journal_mode=WAL`, schema DDL, migrations/backup; old schema may mass-update `tel_match_key` | C/bootstrap. Runtime READ/fallback initialization must be made physically read-only or separated; do not use this constructor against Production during adapter tests. |
| `src/master/reintegration.py`, `repair_reset_job_items(dry_run=False)` | Wide maintenance writes / explicit repair | C; deferred, never called from the Stage4-D runtime selector. |
| Migration/import/reprojection scripts and test/pilot scripts | Writes to staging, sidecar, or test DBs; not ordinary app callsites | C/D; remain separately guarded and must not inherit runtime DB destination implicitly. |

### 4. `lastrowid` and generated IDs

Source SQLite DDL declares these as `INTEGER PRIMARY KEY` without `AUTOINCREMENT`; SQLite aliases them to rowid and supplies the new value. They are not externally assigned identity fields today, but are used as relational keys:

| Source PK | Runtime consumer | Target PK / current default | Proposed Gate 2 contract |
|---|---|---|---|
| `clinics.id` | returned by `_upsert` / review resolve; used by source rows, originals, reviews, history and page/research/job FKs | `public.clinics.id BIGINT PK`, no default/identity sequence | Preserve every imported existing ID. For new rows, PostgreSQL generated ID via `INSERT … RETURNING id`; pass it through every same-transaction FK. Must define rollback collision policy before adoption. |
| `source_records.id` | `_upsert` lastrowid becomes `match_reviews.source_record_id`; may be reused as existing source row ID | `provenance.source_records.id BIGINT PK`, no default/identity sequence | Existing-source update preserves ID. New row uses target-generated `RETURNING id` and review FK uses returned value. |
| `comdesk_original_rows.id` | no direct lastrowid consumer; internal row identity | `provenance.comdesk_original_rows.id BIGINT PK`, no default/identity sequence | Existing imported IDs preserved; new rows need target-generated ID if not explicit allocation. |
| `match_reviews.id` | surfaced to UI and passed to `resolve_review`; no current `lastrowid` read at insert site | `provenance.match_reviews.id BIGINT PK`, no default/identity sequence | Must generate at insert (`RETURNING id`) and return stable ID to UI. |
| `change_history.id` | ordering and `revision()` via MAX(id); no lastrowid read | `provenance.change_history.id BIGINT PK`, no default/identity sequence | Preserve imported IDs; generate target ID for new events; maintain strictly increasing ordering within target, not cross-backend numerical parity. |
| `google_maps_results.id` | no direct lastrowid consumer | `provenance.google_maps_results.id BIGINT PK`, no default/identity sequence | Preserve imported IDs; generate on target for future rows. |
| `search_usage.id` | no direct lastrowid consumer; monthly count only | `research.search_usage.id BIGINT PK`, no default/identity sequence | Preserve imported IDs; generated IDs are internal, monthly semantics unaffected. |
| `national_append_only_import.py` `clinics.id` / `source_records.id` | migration utility uses lastrowid to connect inserted source row | Production SQLite script, not app runtime | C/D only; Stage4-D runtime role must not execute it. |
| `verify_fixed_export.py` clinic ID | fixture/export test only | Temporary fixture | D only. |

Catalog inspection found no sequence defaults on the mapped bigint PK columns above, and no sequences in the mapped schemas. Gate 2 therefore requires an explicit schema migration: identity/sequence generation `BY DEFAULT` (so existing IDs can remain explicitly inserted), seeded above the imported maxima, with generated IDs returned using `RETURNING`. That design is not yet safe for clinic IDs under SQLite WRITE rollback: SQLite may allocate an overlapping next positive rowid after a Supabase-created clinic is not copied back. This rollback collision / read divergence policy is a second unresolved Gate 1.5 blocker. No identity/sequence was created.

### 5. `INSERT OR REPLACE` equivalence map

SQLite triggers were absent in the Production DB; Supabase has no user triggers on the 19 mapped tables. The catalog showed no inbound FK referencing the five replacement targets below. These facts reduce, but do not by themselves erase, SQLite's DELETE+INSERT semantics.

| Runtime statement | Conflict key | Targeted translation | Classification / conditions |
|---|---|---|---|
| `settings` | `key` | `ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value` | A — all columns supplied; no triggers/inbound FK; internal rowid unused. |
| `research_results` | `clinic_id` | `ON CONFLICT(clinic_id) DO UPDATE SET result_json,updated_at` | A — all columns supplied; no triggers/inbound FK; clinic FK points outward to clinics; same row remains. Preserve JSON object merge performed before SQL. |
| `hp_pages` | `(clinic_id,url)` | `ON CONFLICT(clinic_id,url) DO UPDATE SET page_json,checked_at` | A — all columns supplied; save workflow first deletes the clinic's old set, then inserts the new set in the same tx; duplicate URL inputs retain last-row-wins semantics. |
| `manual_overrides` | `(clinic_id,field)` | `ON CONFLICT(clinic_id,field) DO UPDATE SET value_json,source,note,updated_at` | A — all columns supplied; no triggers/inbound FK; explicit `value is None` remains a separate DELETE followed by history. |
| `search_cache` | `query_key` | `ON CONFLICT(query_key) DO UPDATE SET query,result_json,searched_at` | A — all columns supplied; no triggers/inbound FK; only after external search succeeds. |
| `reintegration` research result | `clinic_id` | Same update columns as research_results | C — only if admin path is ever separately approved. |
| `research_worker.page_cache`, hybrid research page/cache tables | sidecar-specific composite keys | Not a 19-table source-table statement | B/D; decide independently with the background Repository. |

`templates INSERT OR IGNORE` should use `ON CONFLICT(id) DO NOTHING`; import batch digest insertion is an idempotency claim and must occur before dependent writes inside the transaction. Current import validation errors abort the whole SQLite transaction. No `INSERT OR REPLACE` occurrence was found on clinics, UUID, or medical_key.

### 6. Transaction boundary contract

| Workflow | Current SQLite boundary | Stage4-D target contract | Failure/recovery behavior |
|---|---|---|---|
| Comdesk/MHLW import | One `BEGIN IMMEDIATE` spans digest check, clinic/provenance writes and batch marker; no external HTTP during transaction | One PostgreSQL transaction; claim digest/idempotency marker first; every imported row and history entry commits atomically | Any validation/constraint failure rolls back entire batch; do not retry automatically. |
| Research job create | One `BEGIN IMMEDIATE` for clinic selection + job + items | One short transaction; job ID remains application UUID hex; clinic IDs are selected from Supabase and inserted as FK rows | Empty selection/constraint failure leaves no job. |
| Job worker lifecycle | Short transactions for job state and item claim; external network/research between transactions; result save and item DONE are currently separate transactions | No transaction during network/API. Use conditional claim UPDATE/RETURNING and commit. After network completion, save result/pages/history/projection and item DONE in a single transaction; error result and terminal item state likewise atomic. | Search attempt reservation is intentionally committed before HTTP and remains charged on network failure. If process dies after HTTP before result commit, retry may repeat external I/O; preserve current recovery but use operation/job-item state to avoid duplicate DB effects. No blind database retry after ambiguous commit. |
| CachedSearch | Transaction reserves monthly quota, increments job search_count and inserts search_usage; commit before external search. Cache upsert is a second transaction after successful response. | Same two short transactions; provider I/O between them; use query_key uniqueness; preserve failed-attempt consumption and no implicit retry. | If cache write fails after API success, return explicit failure or retain/recover result; do not silently repeat paid request. Contract still needs an idempotent response handoff policy. |
| Save research | One `BEGIN IMMEDIATE` for JSON merge, result upsert, optional full page-set replacement, history and projection | One Postgres transaction containing research result, page set, audit row, identity validation and projection update | All or nothing. No Supabase-to-SQLite WRITE fallback or automatic retry. |
| Manual override / review resolution | One `BEGIN IMMEDIATE`; mutation, provenance/review updates, history and projection are atomic; merge uses lock | One Postgres transaction; preserve operation order and expected affected-row counts; duplicate merge stays deferred | Any invalid target/FK/key mismatch rolls back all; report conflict. |
| Google Maps import | One `BEGIN IMMEDIATE` covers batch dedupe, raw result rows, clinic base/projection, history and batch marker; no HTTP inside | One Postgres transaction after frame fetch/validation; commit batch atomically | Duplicate batch no-op; constraint/identity failure rolls back all. |
| Age model refresh | Reads signature; for each clinic independently calls `save_research` (one tx each); sets `age_basis` only after loop | Keep per-clinic atomic transactions, not one giant transaction. Store signature only when every clinic succeeds. | A mid-run failure leaves earlier clinics refreshed and signature old; safe repeat is deterministic but history can duplicate; add idempotency/change detection before Gate2. |
| Treatment/HP research workers | HTTP outside row-result transaction; Treatment worker progress/cache transactions separate; per-clinic Treatment result deletes old set + inserts new + status upsert atomically; HP result upsert commits per clinic and attempts increments | External I/O never holds PG transaction. Each per-clinic result plus status/audit is atomic; operational progress/checkpoint destination must be specified. | Preserve failure attempt/status semantics; no automatic DB retry on ambiguous commit. |

### 7. Audit/history contract

| Mutation | Audit/state rows | Required atomicity/order |
|---|---|---|
| Import NEW clinic | clinic INSERT → `change_history` “医院追加”; then source record/original/review/batch writes | Entire import transaction. Source/history IDs are generated by target. |
| Import MATCHED clinic | base/source update; if base differs, `change_history` “マスター更新”; then source/original/review/batch | Entire import transaction. No history for unchanged base; import batch records idempotency. |
| Research save | `research_results` and optional `hp_pages`; `change_history` “自動調査” always; projection refresh follows | Same transaction; JSON `before` is prior merged research object, `after` is merged result. No `medical_key` update. |
| Manual override | override insert/update/delete; `change_history` “手動修正:<field>” always; projection refresh | Same transaction; before is decoded previous JSON or NULL; after is input value (including NULL delete). |
| Review resolution | update source/review/clinic associations (or create clinic); history “重複確認” after association resolution; projection on non-merge target path | Same transaction. Merge path is deferred because it invokes broad reintegration logic. |
| Google Maps import | raw `google_maps_results`; clinic base update; history “Google Maps取込” only if base changed; projection; batch marker | Same transaction. Preserve confirmed-website anti-downgrade behavior. |
| Job state / search usage/cache / settings | `research_jobs` and item state are operational audit; search_usage records every attempted paid search; settings have no history; no `change_history` entry | Keep transactional job/search state; do not synthesize clinic history events that SQLite does not currently emit. |
| Treatment / HP background result | sidecar status/result, attempts/progress; no current `change_history` | Keep source-specific audit/progress semantics; do not add provenance history unless separately approved. |

All timestamps produced by `store.now()` are UTC ISO-8601 strings with microseconds. Google Maps uses UTC ISO-8601 (default precision); legacy imported timestamps remain strings. JSON source tables currently store compact, sorted UTF-8 JSON text in SQLite; migrated JSONB fields must be parsed before comparison/serialization, while text-JSON fields remain text. SQLite booleans are integer 0/1; Postgres booleans must be explicit conversions.

### 8. Runtime DB role proposal (design only)

READ ONLY catalog facts: all 19 target tables currently have RLS enabled and no policies; target tables are owned by `postgres`; mapped schemas have no sequences; user triggers are absent. The inspected current pooler login is `postgres`, with `rolbypassrls=true`, `rolcreatedb=true`, and `rolcreaterole=true`. `anon`, `authenticated`, and `PUBLIC` have no INSERT privilege on `public.clinics`; prior Stage4-C security result is ERROR 0 / WARNING 0.

Proposed dedicated server-side login, e.g. `clinic_runtime` (name subject to Supabase role naming/pooler support): `LOGIN`, `NOSUPERUSER`, `NOBYPASSRLS`, `NOCREATEDB`, `NOCREATEROLE`, `NOINHERIT`; no ownership, DDL, role membership, or frontend exposure. It would receive `USAGE` only on `public`, `provenance`, `research`, `app_config`, `treatment`, `hp_research` as required by A+B, SELECT only on required mapped tables, and per-table INSERT/UPDATE/DELETE limited to audited operations. Identity sequences, once explicitly created, get only required `USAGE`/`SELECT` privileges. No grant or policy to `PUBLIC`, `anon`, or `authenticated`.

Since target tables have RLS enabled with no policies, add table-specific policies `TO clinic_runtime` for only required commands (not PUBLIC), with row predicates narrowly reflecting server-side app scope. No general permissive policy to client roles. Before cutover, test every operation under the non-owner NOBYPASSRLS login through Session Pooler; security advisor must remain ERROR 0 / WARNING 0. No role, grant, or policy was created.

### 9. Mandatory/deferred scope and remaining blockers

**A — Stage4-D mandatory UI/runtime:** settings; Comdesk/MHLW imports; new and matched clinic updates; manual override; review resolution that does not merge; Google Maps result import; research result/page save; job create/pause/reset/limit/claim/finish; search quota/usage/cache; age model refresh. Every callsite, including `_create_maps_hp_job`, must use the write Repository.

**B — Stage4-D mandatory enabled background runtime:** Treatment Research worker result/status writes and operational progress/cache; HP batch result/attempt writes and checkpoints; any deployed national Maps/source collection queue. Either route to Supabase with an explicit schema/repository or formally prove a component is offline/not enabled in production. A/B SQLite business writes cannot remain at Stage4-D PASS.

**C — deferred admin/maintenance:** `reintegrate_existing`, persistent `repair_reset_job_items`, reprojection and recovery tools, `ClinicStore` schema/bootstrap migration behavior, backup/restore. These remain behind explicit admin/offline commands and are never selected by runtime backend flag.

**D — deferred test/migration:** full shadow importer, one-off migration/apply, pilot/canary fixtures, test data creators and temporary benchmark writers.

Gate 1.5 remains **NO-GO** for two unresolved contract decisions:

1. Existing blank `medical_key` may currently become known on matched official MHLW input. Strict identity immutability means that operation must fail or behavior must be explicitly changed; there is no way to preserve both behaviors. The owner must choose the intended rule.
2. Supabase-generated new clinic integer IDs can collide with IDs SQLite would generate after a WRITE rollback, while READ remains Supabase and successful Supabase mutations are not copied back. The application needs an approved disjoint ID allocation/rollback-read policy, or must defer new-clinic creation from the Supabase WRITE scope (which means Stage4-D cannot claim all normal runtime WRITE is cut over).

Also unresolved before Gate 2: idempotent recovery for “external API succeeded, result transaction failed/commit outcome unknown”; age-refresh history duplication under partial failure; which of the collection queues are actually enabled in production; exact per-table NOBYPASSRLS policies/grants under a real runtime login. These can be closed with design/test contracts but require explicit evidence before implementation/canary.

### Gate 1.5 result

Audit, schema inspection, and documentation only. Production SQLite SHA remained unchanged. Supabase remained clinics = 162,258 and 19 mapped tables = 632,903; row data writes = 0. No source code, adapter, role, grant, policy, or sequence was modified/created. Because the existing-key evolution and new-ID rollback behavior are unresolved, **Stage4-D Gate 1.5 = NO-GO** and **Gate 2 = NO-GO**. Stop here; do not implement an adapter or run any canary until those decisions are approved.

## Gate 1.5 — independent verification addendum (Claude session, READ ONLY, handoff continuation)

Date: 2026-10-07. Starting HEAD confirmed `214b48d`, branch `feature/supabase-shadow-migration`, `git status --short` clean of tracked-file changes, `git diff --check` empty. This addendum re-verifies the audit above directly against source code and the live catalog before accepting it as the basis for the final Gate 1.5 decision. Nothing in this addendum performed a write; no adapter, role, grant, policy, or sequence was created.

**Code re-verification (read-only `Read`/`grep`, no execution):**

- `src/master/matching.py:17-24` `medical_key()` matches the description exactly.
- `src/master/store.py:325-365` `_project()` confirmed: `medical_key` is computed via `medical_key(data)` and is always included in the `UPDATE clinics SET ...` column list (line 353/365). There is no code path in `_project` that excludes it.
- `src/master/store.py:367-421` `_upsert()` confirmed: for `MATCHED` with `source=="厚生局"` (MHLW) and newer `as_of`, `base.update(record)` can introduce fields — including `clinic_id` — that were previously absent from `base`, and `_project(c,cid)` is unconditionally called afterward (line 420) for any resolved `cid`. This is the exact mechanism by which an existing clinic's stored `medical_key` can move from blank to non-blank. Codex's finding is confirmed from the code, not only from the 162,258-row snapshot audit.
- `INSERT OR REPLACE`/`INSERT OR IGNORE` occurrences in the full active source tree (`src/master/store.py:285,440,487,491,524`; `src/enrichment/search_provider.py:86`) match the five-statement list in Gate 1.5 §5 exactly; no `INSERT OR REPLACE` touches `clinics`, confirmed by full-tree grep.
- `lastrowid` occurrences (`src/master/store.py:396,402,694`) match §4; no other active-tree callsite was found.
- `app_v2.py:493-523` `_create_maps_hp_job` confirmed: inline `BEGIN IMMEDIATE`, direct SQL, bypasses `ClinicStore` Repository methods, as described.
- `src/enrichment/search_provider.py` `CachedSearch.search` confirmed: reservation transaction (`search_usage` INSERT + job `search_count` UPDATE) commits **before** the network call; `search_cache` `INSERT OR REPLACE` happens in a second transaction **after** the network call; a failed/charged attempt is not refunded. Matches §6 exactly.
- `src/master/jobs.py:86-91` `repair_reset_job_items(store, job_ids, dry_run=True)` confirmed default `dry_run=True`, and dry-run path always rolls back inside the same `BEGIN IMMEDIATE`.
- `src/master/reintegration.py:133,185-187` confirmed: `FileLock`-guarded `reintegrate_existing`, and a full `UPDATE clinics SET tel_match_key=...` sweep exists in the legacy-schema upgrade path, both correctly scoped as Class C/deferred.
- `src/master/hp_research_batch.py:232-258` confirmed idempotent `ON CONFLICT(clinic_id) DO UPDATE` with `attempts = prior_attempts + 1`, matching §9's Class B description.
- **New clarification not fully closed in the handed-off draft:** `src/enrichment/kouseikyoku_source.py` has **no SQL/database code at all** — it only fetches/parses MHLW Excel/HTML into a `DataFrame` that is then passed into the already-covered `import_master` Class A path (`app_v2.py` imports `load_master` from this module). It introduces no independent write boundary and needs no separate classification.
- **New clarification not fully closed in the handed-off draft:** `src/master/national_maps_queue.py` is used only by `national_maps_3pc_app.py`, a separate Streamlit entrypoint (distinct from `app_v2.py`) that explicitly states and implements "この画面は clinics.sqlite3 を更新しません" — it opens the production DB strictly read-only (`load_completed_maps_ids_readonly`) and writes only local staging/output CSV files outside any of the 19 mapped tables. This resolves one of the "remaining blockers" sub-questions in §9: `national_maps_queue.py` is **Class D (offline collection tool)**, not Class B, and is out of Stage4-D mandatory runtime scope. `src/enrichment/kouseikyoku_source.py` requires no independent classification (see above). `src/master/sales_classification.py` was independently re-read and confirmed: it never opens `clinics.sqlite3` for write, builds a rebuildable sidecar via a `tmp` file, and is correctly excluded from WRITE Primary accounting.

**Live catalog / safety re-verification (read-only session, `SET default_transaction_read_only = on` set explicitly before any query; connection via the pre-exported `SUPABASE_DB_URL` Session Pooler, value never displayed):**

| Check | Result |
|---|---|
| `default_transaction_read_only` | `on` (set explicitly this session) |
| `pg_is_in_recovery()` | `false` |
| `current_user` / `session_user` | `postgres` / `postgres` |
| `public.clinics` count | 162,258 — unchanged |
| Sum of 19 mapped tables | 632,903 — unchanged (per-table breakdown matches Codex's figures exactly, e.g. `provenance.change_history`=179,105, `treatment.clinic_treatment_research`=79,650, `hp_research.clinic_hp_research`=10,313) |
| Sequences in `public`,`provenance`,`research`,`app_config`,`treatment`,`hp_research` | none |
| RLS on all 19 target relations | `relrowsecurity=true`, `relforcerowsecurity=false`, confirmed for every one |
| `pg_policies` on those schemas | none |
| Non-internal triggers on those schemas | none |
| Inbound FKs to `settings`,`research_results`,`hp_pages`,`manual_overrides`,`search_cache` | none for any of the five |
| Current login role attrs | `rolsuper=false, rolinherit=true, rolcreaterole=true, rolcreatedb=true, rolcanlogin=true, rolbypassrls=true` |
| INSERT privilege on `public.clinics` for `anon`/`authenticated`/`PUBLIC` | none |
| Table ownership | all 19 target tables owned by `postgres` |

Production SQLite SHA-256 was independently recomputed by whole-file hash (`shasum -a 256`) against the three live production paths and matches the supplied baseline exactly:
- `clinics.sqlite3` → `fc53c9de851567c62f75a18aceb9521cb94b82385e69c19f09e880df386cc8a0`
- `treatment_research_final.sqlite3` → `36dbe570d19af61ca45c8571338a2cb3e3c28e472b42113286e0a0e093add804`
- `hp_abc_batch_sidecar.sqlite3` → `3980865ebd0de6f30dcebad98b1aada92252b00b3e7836874b408ffa1dbf7d53`

One out-of-scope observation (not a blocker, informational only): the live `public` schema also contains `clinic_email_enrichment`, `new_clinic_candidate_evidence`, `new_clinic_candidates`, and `new_clinic_source_records` with RLS enabled and no policies — these are not part of the 19-table Stage4-D mapping and were not otherwise audited; they should be explicitly scoped in or out before any future runtime role/grant design touches `public`.

**Conclusion of independent verification:** every factual claim in Codex's Gate 1 / Gate 1.5 audit that was checked against source code or the live catalog is confirmed accurate. No contradiction was found. The two blockers Codex identified are not gaps that further investigation closes — they are genuine, mutually exclusive business/identity decisions that only the system owner can make. The owner decisions resolving them are recorded below.

## Owner Decision (2026-10-07) — closes the two Gate 1.5 blockers

The system owner reviewed both blockers above and made the following two binding decisions. Neither was chosen unilaterally by the assistant; both are recorded verbatim in effect, with the supporting verification performed this session.

### Decision 1 — `medical_key` identity contract (adopts option (b))

**Existing clinic, `medical_key` non-blank:** immutable. `known → other known` and `known → blank` are both forbidden under every ordinary runtime mutation (`save_research`, manual override, review resolve, Google Maps import, projection refresh, research, any other clinic UPDATE). If a recomputed candidate key differs from the stored non-blank key, the mutation must not update `medical_key`; it is treated as an error/audit event, not a silent repair.

**Existing clinic, `medical_key` blank:** `blank → known` is allowed **exactly once**, and only when an authoritative MHLW (`厚生局`) source matches the existing clinic and legitimately resolves the key under the same conditions as current Production semantics (this is the `_upsert` MATCHED path confirmed at `store.py:369-391` and the `resolve_review` MATCHED path confirmed at `store.py:696-705`). This is classified as **identity completion**, not identity mutation. Once the key becomes known, it immediately falls under the non-blank immutability rule above — no further transition is ever allowed.

**New clinic:** `medical_key` may be generated only at initial INSERT, only from a validated official source. After INSERT, the row is subject to the existing-clinic contract above.

**Final rule table:**

| Transition | Allowed? | Condition |
|---|---|---|
| blank → known | Yes, once | Authoritative MHLW match (existing clinic) or initial official creation (new clinic) only |
| known → other known | No | Never |
| known → blank | No | Never |

**`_project()` split (confirmed design, still unimplemented):** must separate (1) pure projection derivation, (2) identity completion (blank→known, callable only under the authoritative-match condition), (3) identity validation (equality-only, raises on mismatch, no write), and (4) projection refresh restricted to non-identity columns. This matches §2 above; Gate 2 must implement it exactly this way, not as a single `_project()` that always writes `medical_key`.

### Decision 2 — Postgres-generated numeric ID namespace (reserved high range)

To prevent a Supabase-generated new-row ID from later colliding with an ID SQLite would independently allocate after a rollback to `CLINIC_WRITE_BACKEND=sqlite` (since successful Supabase writes are never copied back to SQLite — dual-write stays forbidden), Supabase-side runtime-generated numeric IDs are reserved to a disjoint high range:

- **Supabase runtime-generated IDs:** `>= 1,000,000,000`.
- **SQLite rollback-mode new IDs:** continue using the existing low range unchanged.
- **Sequence start value:** `next_id >= max(1_000_000_000, MAX(existing_id) + 1)` — existing imported IDs are never renumbered.
- **Generation mechanism:** `GENERATED BY DEFAULT AS IDENTITY` (or an explicit sequence with equivalent behavior) — **not** `GENERATED ALWAYS`, so migration/admin paths can still explicitly insert/preserve historical IDs.
- **INSERT contract:** `INSERT ... RETURNING id` replaces SQLite `lastrowid` semantics everywhere a generated ID is consumed.
- **Guard:** if a Supabase-generated ID is ever `< 1,000,000,000`, that is treated as an implementation failure, not a silent acceptance. SQLite rollback mode never generates a high-range ID.

**Scope — which tables actually need this (re-derived from the three live `lastrowid` callsites, not applied blanket across all 19 tables):**

| Callsite | Table / PK | Why it needs the reserved range |
|---|---|---|
| `store.py:396` (`_upsert`, NEW branch) | `public.clinics.id` | Generated ID is captured and immediately reused in the same transaction (`source_records`, `comdesk_original_rows`, `match_reviews`, history all FK to it). |
| `store.py:402` (`_upsert`, new source record) | `provenance.source_records.id` | Generated ID is captured and immediately reused as `match_reviews.source_record_id` in the same transaction. |
| `store.py:694` (`resolve_review`, NEW branch) | `public.clinics.id` | Same mechanism as the first row — a second code path that creates a new clinic row and immediately reuses the generated ID. |

Only **`public.clinics`** and **`provenance.source_records`** have application code that captures a newly generated numeric ID and relies on that specific value within the same operation. These are the two tables that get the reserved `>= 1,000,000,000` sequence in Gate 2.

**Residual note (not a blocker, factual scope boundary only):** `provenance.match_reviews`, `provenance.comdesk_original_rows`, `provenance.change_history`, `provenance.google_maps_results`, and `research.search_usage` also insert new rows without the application ever supplying or reading back an `id` value — they still need *some* Postgres ID-generation mechanism (a plain sequence, default range, no reserved floor required) for Gate 2, since no application code cross-checks their generated value against a rollback-divergence scenario. Per the owner's explicit instruction, the reserved high-range floor is **not** applied to these five tables unless a future audit finds a callsite that captures and reuses their generated ID the same way. `research.research_jobs.id` and `research.research_job_items` use an application-generated UUID hex / composite key respectively — no integer-PK collision risk exists for either, confirmed in `jobs.py`.

### High-range ID compatibility guard — checked this session, PASS

Read-only source-tree search for anything that would reject or mishandle an ID `>= 1,000,000,000`:

- No fixed-width/zero-padded formatting of any `id`/`clinic_id`-PK column (`zfill`/`%0Nd`/`:0Nd` occurrences found are unrelated — JIS prefecture codes, HH:MM time formatting, sample-data phone numbers).
- No `int32`, `struct.pack`, bitmask (`0xFFFFFFFF`), or bit-length assumption on any ID anywhere in the active source tree.
- No `CHECK` constraint or digit-count regex restricting `id`/`clinic_id`-PK column values; all are plain SQLite `INTEGER PRIMARY KEY` (unbounded within 64-bit) mapping to Postgres `BIGINT` (confirmed in §4), which holds values far beyond `1,000,000,000` with no practical ceiling.
- No `number_input`/`max_value` UI control is bound to the internal `clinics.id`/`source_records.id` PK (the only `number_input`/`max_value` usages found are page counts and batch-size limits, unrelated to identity columns).
- No pandas/numpy `int32` dtype cast, no API schema layer (no FastAPI/pydantic in this codebase), and no custom JSON integer handling in `dumps()` that would truncate a value this size.
- Comdesk export (`comdesk.py:new_export_row`) only ever writes `clinic_name`/`phone`/`address`/`prefecture` into mapped columns — the internal numeric `clinics.id` is never written into a Comdesk export row at all.

**Conclusion: `1,000,000,000` is compatible with the existing runtime, UI, and Comdesk/export code as audited. No incompatibility was found; the guard does not trigger a STOP.**

### Gate 1.5 — finalized status

| Item | Status |
|---|---|
| `medical_key` contract | Finalized (Decision 1) |
| Existing/new clinic identity contract | Finalized (Decision 1) |
| `_project()` responsibility split | Finalized design (unimplemented) |
| Numeric ID generation contract | Finalized (Decision 2) |
| `lastrowid` mapping | Finalized — only `public.clinics.id` and `provenance.source_records.id` need the reserved range |
| `INSERT OR REPLACE` mapping | Finalized (§5, re-verified against live code this session) |
| Transaction boundary contract | Finalized (§6) |
| Audit/history contract | Finalized (§7) |
| Runtime least-privilege role design | Finalized design (unimplemented — no role/grant/policy created) |
| Mandatory (A+B) / deferred (C/D) WRITE scope | Finalized (§9, with `national_maps_queue.py`/`kouseikyoku_source.py` clarified this session) |

No new contradiction was found against any of the above when integrating the two Owner Decisions. Production SQLite SHA unchanged, Supabase `public.clinics` = 162,258 and 19 mapped tables = 632,903, Supabase row data writes performed this session = 0.

**Stage4-D Gate 1.5 = PASS.**
**Gate 2 (WRITE Repository / Adapter Implementation) = GO, as a design/implementation-authorization decision.**

This PASS authorizes *starting* Gate 2 engineering work (Repository contract code, version-controlled migration SQL drafts). It is not, by itself, a record of any DDL, role, grant, policy, sequence, canary, or WRITE having been executed against the live Supabase project — none were, this session. See the assistant's reply in-conversation for why unattended execution through DDL/canary/cutover was not started without the owner present.
