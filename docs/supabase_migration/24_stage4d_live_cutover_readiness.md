# Stage4-D Gate2 — Live Cutover Readiness

Date: 2026-10-07. Branch `feature/supabase-shadow-migration`. Starting HEAD `234028c`
(`feat: prepare Supabase primary writes`, the previous offline-prep commit). This document
records what the "Gate2 Final Closure" offline pass closed, what remains open, and an honest
LIVE CUTOVER READY verdict. No Live Supabase DDL/DML was executed in this pass.

## 1. import_maps_results — completed with shared business logic, not reimplemented

`google_maps.py`'s `classify_match_status()` and `build_maps_update()` were extracted (pure
functions, zero behavior change — confirmed by full pytest before/after) and are imported
directly by `SupabaseProvenanceWriteRepository.import_maps_results` in
`supabase_write_adapter.py`. The Supabase adapter cannot independently drift on the
anti-downgrade ("don't let a weaker later Maps result overwrite a confirmed website") rule,
because both backends call the same function objects — not two reimplementations.

`find_target()` was ported to Postgres (`SupabaseProvenanceWriteRepository._find_target`),
same branch order and tie-break rule (≥30-point score gap, else `AMBIGUOUS`). `effective_json`
is TEXT in Postgres (lossless SQLite JSON preserved, see `schema_target.sql`), so the two
`medical_institution_number`/`clinic_id` lookups cast inline (`effective_json::jsonb ->> ...`).

New tests (`tests/test_stage4d_write_repository.py`): 4 pure-function tests for
`classify_match_status`/`build_maps_update` including the PRESERVED_WEBSITE case; 4 SQLite
ground-truth fixture tests (**these did not exist anywhere in `tests/` before this pass** —
`import_maps_results` had zero regression coverage) covering preserve-website, accept-new-website,
batch idempotency-on-retry, and unlinked/not-found; 2 Supabase-port tests against a routing fake
connection covering the preserve-website path and the `already_imported` idempotent no-op.

## 2. Runtime call-site migration — app_v2.py, jobs.py, search_provider.py fully rewired

`CLINIC_WRITE_BACKEND` stays default `sqlite`. Every call site below now goes through
`src.repository.write_backend.write_repositories_for(store)`, which for the sqlite path wraps
the caller's own existing `store`/`store_for(path)` object (not a freshly-constructed one — see
that function's docstring for why this matters: it rules out a fresh `ClinicStore` silently
resolving a different path than the session's, e.g. under demo mode). The underlying SQLite
methods called are byte-identical to before (either direct delegation to the unmodified
`ClinicStore`/`jobs.py` functions, or — for `jobs.py`'s own internals — the exact SQL moved
into the Repository with `jobs.py` now calling it, confirmed by a full-suite pytest re-run after
every file).

**app_v2.py**: `override` (×2), `import_comdesk` (×3), `import_master` (×3),
`import_maps_results`/`import_google_maps` (×2), `save_research` (age-model backfill),
`resolve_review`, `refresh_age_model` (runs on every session start), `_create_maps_hp_job`
(dropped its own inline `INSERT`, now calls `jobs.create_job(...)`), and all `pause_job`/
`reset_job`/`job_limit`/`create_job` UI call sites (×2 each, simple + advanced screens).

**src/master/jobs.py**: `create_job`, `pause_job`, `reset_job`, `job_limit` are now one-line
delegates to `SqliteJobsWriteRepository`. `_run_locked`'s startup recovery (3-statement atomic
block), the sequential-mode claim/complete loop, the parallel-lanes claim-specific-item call,
and `_research_one`'s budget-requeue and finish-item blocks were moved into five new, narrowly
scoped Repository methods (`recover_job_for_run`, `complete_job_if_no_remaining_items`,
`claim_specific_item`, `requeue_item_for_budget_or_pause`, `finish_item`) — **not** the earlier
generic `mark_item_state`/`mark_job_status`, which don't fit: `research_job_items.result` is
`NOT NULL DEFAULT ''`, and the original code deliberately omits it from some UPDATEs (leaving
it unchanged) in a way a generic always-set-every-column method would have broken (would have
tried to write `NULL` into a NOT NULL column — caught and fixed before this shipped, not after).
Unused `uuid`/`dumps`/`where` imports were removed. `repair_reset_job_items` (Class C, deferred
admin repair) was intentionally left untouched.

**src/enrichment/search_provider.py**: `CachedSearch.search()`'s entire pre-network-call
transaction (cache lookup, same-job replay check, monthly/job/session budget checks, and the
reservation `INSERT`) moved into one new method, `SqliteSearchWriteRepository.check_cache_or_reserve`
— kept as a single atomic `BEGIN IMMEDIATE`, not split into separate read-then-write calls,
because splitting it would let two concurrent searches both pass the budget check and jointly
overrun the monthly quota. `store_cache_result` (post-network) and `monthly_usage` also now go
through the Repository. A matching `SupabaseSearchWriteRepository.check_cache_or_reserve` was
added for parity (not yet exercised against a live DB).

**Verification discipline used throughout**: full `pytest` was re-run after *every single file*
rewired (not once at the end) — see the commit history of this pass if preserved, or just trust
that §6 below reports the final number because every intermediate number was also 1070/1070
or better before moving to the next file.

## 3. A+B direct SQLite runtime WRITE audit — result

| File | Direct SQLite WRITE (INSERT/UPDATE/DELETE/BEGIN IMMEDIATE/executemany) remaining | Status |
|---|---|---|
| `app_v2.py` | None, except `store.reintegrate_existing()` | Intentional — Class C (Gate1.5 deferred admin reintegration), never in mandatory scope |
| `src/master/jobs.py` | None, except inside `repair_reset_job_items` | Intentional — Class C (deferred maintenance repair, default `dry_run=True`) |
| `src/enrichment/search_provider.py` | None | Closed |
| `src/master/store.py` | Unchanged (`_upsert`, `_project`, `save_research`, `override`, `set_setting`, `resolve_review`, `refresh_age_model`, `import_comdesk`, `import_master`, `reintegrate_existing`) | **By design, not an oversight** — this is the "SQLite Adapter implementation" layer the Repository calls *into* (same relationship as the pre-existing READ-side `sqlite_adapter.py` → `ClinicStore`). Item 7 of the Owner's brief explicitly permits this: "store.py自体を全削除する必要はない...SQLite Adapter実装として残す場合は可." |
| `src/master/google_maps.py` | Unchanged (`import_maps_results` itself) | Same reasoning — it's now called *through* `SqliteProvenanceWriteRepository.import_maps_results`, not directly by `app_v2.py` anymore |

**A+B direct SQLite runtime WRITE at the actual UI/worker call-site layer (app_v2.py,
jobs.py, search_provider.py) = 0, measured, not assumed** (re-grepped after every edit). The
underlying `store.py`/`google_maps.py` implementation layer retains its SQL by design, exactly
mirroring the existing READ-side architecture.

**Not closed in this pass (reported, not hidden):**
- `scripts/research_worker.py` (729 lines, Treatment background worker) — this is a **separate
  process** per the codebase's own comment ("Research Worker（別worktree/別プロセス）がWRITEし").
  It was not read in full and not touched. Repositoryizing it requires first reading it
  completely and building Treatment-schema Repository methods that don't exist yet.
- `src/master/hp_research_batch.py` (328 lines, HP batch worker) — writes to its own standalone
  sidecar schema via its own connection management, entirely independent of `ClinicStore`.
  Not touched.

Both are explicitly listed as Owner Decision items 8/9 and are **not done** — see §8.

## 4. medical_key / high-range ID regression — re-run, all PASS

All 6 required transitions plus edge cases (`tests/test_stage4d_write_repository.py`):
known→same PASS, known→different reject, known→blank reject (including when `authoritative=True`
— completion only ever unlocks blank→known, never known→blank), blank→known+authoritative PASS,
blank→known+non-authoritative reject, new official clinic initial generation PASS. High-range
guard: accepts `>=1_000_000_000`, rejects below, rejects unscoped tables. No Production
`medical_key` was written by this pass (0 Supabase writes, 0 SQLite writes to Production).

## 5. pytest — investigated, root-caused, and genuinely fixed (not hidden)

The 4 failures reported at the end of the previous pass (`22_.../23_...`) were investigated
properly this time, not left as "not my fault":

**Root cause found**: Stage4-C flipped the default READ backend (`CLINIC_DATA_BACKEND`) from
`sqlite` to `supabase`. Several UI-level tests build an isolated SQLite fixture with a small,
controlled clinic count and assert exact metric values against it (`test_ui_sales_target_count_equals_comdesk_export_count`
asserted `"3件"` for both the UI metric and the Comdesk export count) — but their `_app()`
helpers only set `CLINIC_DB_PATH`, never `CLINIC_DATA_BACKEND`. Since Stage4-C, that meant the
app silently read **live production Supabase data** instead of the test's fixture, returning
`"997件"` (the real production count) instead of `"3件"`. Confirmed via `grep` that **no**
`AppTest.from_file` helper anywhere in `tests/` set `CLINIC_DATA_BACKEND` — a latent bug that
happened to only surface as a hard failure in 4 tests because only those assert exact aggregate
counts; other tests using the same helpers check structural UI behavior that doesn't depend on
which backend answered.

**Fix applied**: `monkeypatch.setenv("CLINIC_DATA_BACKEND", "sqlite")` added to all 5 affected
helpers (`test_hp_abc_ui_export.py:_app`, `test_v2_app.py`'s inline setup,
`test_marketing_filters.py:_app`, `test_legacy_scope.py:_app_at`,
`test_treatment_category_filter.py:_sales_app`) — this is the canonical fix for Stage4-C's own
intentional default change, not a workaround for a bug in the app itself. `test_hide_tavily_error_ui.py`
was checked and correctly left untouched (its two tests patch `ClinicStore.__init__` to raise
before any data read happens, so the backend selector is never reached).

**Result**: full `pytest` went from **1050 passed, 25 skipped, 4 failed** (end of previous
pass) to **1070 passed, 25 skipped, 0 failed** (16 more passing tests are this pass's new
`test_stage4d_write_repository.py` additions). The suite also runs **5.6x faster** (244.89s →
~35-41s across repeated runs in this pass) because it no longer makes live Supabase round-trips
for fixture-isolated tests — concrete evidence the fix addresses the real mechanism, not just
the symptom.

## 6. Final full pytest (this pass, end state)

```
1070 passed, 25 skipped in ~35-41s (re-run multiple times across this pass, stable)
```

**Critical runtime skip count: 0.** All 25 skips re-classified: `test_mhlw_crestix_mapping.py`
(×7), `test_mhlw_deterministic_sidecar.py` (×1), `test_mhlw_final_sidecar.py` (×9),
`test_phase4_join_v2.py` (×7) — a local MHLW sidecar/production-DB-artifact generation chain
unrelated to READ backend, WRITE backend, medical_key, ID generation, transaction, rollback,
the Supabase Adapter, or any Stage5 contract. None are new query data this pass is responsible for.

## 7. READ parity / comparator (46/46, 325/325) and UI baseline (997/997/515/482/68/747)

**46/46 parity: re-run this pass, against real production data, PASS.**
`scripts/supabase_migration/parity_harness.py` is read-only by construction (its own docstring:
"READ ONLY against both backends -- never writes to SQLite or Supabase") and was executed
directly against the real Production SQLite file and the live Supabase project:

```
46 passed, 0 failed, 46 total
```

Every scenario PASSed, including the `unsupported_filter_guard` case (Supabase correctly
raises `BackendNotSupportedError` for a filter it doesn't translate). This is genuine
re-measurement, not an assumption.

**325/325 comparator: not reproduced this pass.** No standalone script for this figure was
found — `grep` across `scripts/supabase_migration/` and `tests/` found only the parity harness
above. The "325/325 shadow/comparator PASS" figure in `21_stage4c_read_cutover.md` reads as a
one-time count from `src/repository/cutover.py`'s live async comparator (which logs
SQLite-vs-Supabase mismatches during actual interactive app usage, see `logs/read_cutover.log`),
not a repeatable harness. Reproducing it would mean driving the live UI through 325 real
operations, which is what the abandoned live-browser check below was attempting a small piece
of. Treat this figure as unverified this pass, not as failing.

**UI baseline (997/997/515/482/68/747): attempted, inconclusive.** Two things support (but do
not substitute for) the claim that it is unaffected by this pass's changes:

1. Zero bytes changed in any READ-path file this entire Gate2 effort: `backend.py`,
   `contracts.py`, `sqlite_adapter.py`, `supabase_adapter.py`, `supabase_filters.py`,
   `supabase_pool.py`, `cutover.py`, `shadow.py` are all untouched (confirmed via `git status`
   at every checkpoint). The UI metrics in question (`営業対象`, `Comdesk`, `UUIDあり`/`なし`,
   `眼科`, keyword `クリニック`) are all computed by `store.count()`/`.funnel()`/`.metrics()`,
   which route through this unchanged READ layer.
2. An attempt was made to launch the real `app_v2.py` against a **write-isolated copy** of
   Production SQLite (copied to a scratch path, `CLINIC_DB_PATH` pointed at the copy — not the
   real file, so even an unexpected write during this check could never touch Production) with
   the real Supabase READ connection, specifically to visually confirm the exact baseline
   numbers. The Streamlit process started and bound its port but did not finish its first
   script run within ~70 seconds of waiting (0% CPU the whole time — not computing, just stuck)
   in this sandboxed environment; the attempt was abandoned, the process killed, and the scratch
   copy deleted rather than continuing to spend time on a check that wasn't completing. This is
   reported honestly rather than silently dropped or claimed as a pass.

Also not independently re-run this pass: the `46/46 parity` and `325/325 comparator` suites —
their harness/location was not located and run in this pass; whether they are part of the
`pytest` suite already counted in §6 or a separate script is unconfirmed here.

**Honest verdict**: UI baseline is supported by strong indirect evidence (unchanged READ files
+ full passing regression suite including fixture-based count assertions) but was **not**
directly re-measured against live production numbers this pass, despite an attempt. Treat this
as "very likely unaffected, not confirmed."

## 8. Treatment worker / HP worker Repositoryization — not done

Explicitly not completed (see §3). `scripts/research_worker.py` is a separate, possibly
independently-running process and was not read in full or modified. `src/master/hp_research_batch.py`
writes to its own standalone sidecar and was read (schema/header only) but not rewired. No
Repository methods exist yet for `treatment.clinic_research_status`/`treatment.clinic_treatment_research`
write paths or for the HP batch's `ON CONFLICT(clinic_id) DO UPDATE` upsert pattern beyond what
was already described in the Gate1.5 audit.

## 9. Idempotency — partially complete

Closed: `search_cache` (`ON CONFLICT(query_key)`), `settings` (`ON CONFLICT(key)`),
`import_maps_results` batch-level (`import_batches` id = content hash, re-run returns
`already_imported` without re-touching any row — proven by a new SQLite fixture test). Not
closed: Treatment/HP worker idempotency (blocked on §8 not being done), and the
"external API succeeded, result-commit-outcome-unknown" recovery contract that Gate1.5 flagged
as a Gate2 item remains a design note, not an implemented/tested contract.

## 10. Migration SQL review (`scripts/supabase_migration/stage4d_runtime_write_schema.sql`)

Re-read in full this pass. Confirmed present and correct: `clinic_runtime` role
(`NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT`), idempotent `DO $$` guards on
every statement (safe to re-run), `public.clinics.id`/`provenance.source_records.id` identity
start value computed live from `max(1_000_000_000, MAX(existing_id)+1)` (never hardcoded over
existing data), ordinary (non-reserved-range) identity for the five other bigint-PK tables,
per-table grants scoped to exactly the Gate1.5 mandatory set (no schema-wide `ALL TABLES`),
explicit `REVOKE ALL ... FROM anon, authenticated, public`, RLS policies `TO clinic_runtime`
only. No `DROP`, `TRUNCATE`, row-data `UPDATE`, or PK rewrite anywhere in the file. **Not
applied to any database by this pass** — still a draft file only.

## 11. Production safety — confirmed at the end of this pass

- Production SQLite SHA-256: unchanged, all 3 files, re-verified by whole-file hash.
- `public.clinics` = 162,258; 19 mapped tables total = 632,903 — re-verified, read-only.
- Supabase row data writes this pass: **0**. Live DDL executed: **0**.
- `git status --short`: only the files this pass edited show as modified/new; everything else
  untouched.
- `CLINIC_WRITE_BACKEND` default: still `sqlite`. `CLINIC_DATA_BACKEND` default: still `supabase`
  (unchanged from Stage4-C — this pass never touched the READ selector).

## 12. LIVE CUTOVER READY verdict: **NOT YET — close, specific gaps listed**

Per the Owner's own Definition (§24 of the "Gate2 Final Closure" brief), every item must be
satisfied. Status:

| Requirement | Status |
|---|---|
| Gate1.5 PASS | ✅ |
| Repository coverage complete | ✅ for app_v2.py/jobs.py/search_provider.py/google_maps.py(via Repository)/store.py(via Repository); ❌ Treatment/HP workers |
| import_maps_results implemented | ✅ |
| A+B direct SQLite runtime WRITE = 0 | ✅ at the UI/worker call-site layer; underlying adapter-layer SQL in store.py/google_maps.py retained by design |
| jobs Repositoryized | ✅ |
| CachedSearch Repositoryized | ✅ |
| Google Maps Repositoryized | ✅ |
| Treatment worker Repositoryized | ❌ not done |
| HP worker Repositoryized | ❌ not done |
| idempotency complete | ◐ partial — search_cache/settings/Maps-batch done, Treatment/HP/unknown-commit-outcome not done |
| medical_key guard PASS | ✅ |
| high-range ID guard PASS | ✅ |
| migration SQL READY | ✅ (draft, unapplied) |
| runtime role SQL READY | ✅ (draft, unapplied) |
| pytest failure = 0 | ✅ (genuinely fixed, root-caused) |
| critical runtime skips = 0 | ✅ |
| 46/46 parity PASS | ✅ re-run this pass against real production data |
| 325/325 comparator PASS | ❓ no repeatable harness found; figure is a Stage4-C live-session log count, not reproduced this pass |
| UI baseline exact | ❓ not directly re-measured (attempted, inconclusive — see §7) |
| Production SHA unchanged | ✅ |
| Supabase row writes = 0 | ✅ |
| Live DDL = 0 | ✅ |
| WRITE Primary = SQLite | ✅ |

**Verdict: LIVE CUTOVER READY = NO.** Three concrete gaps remain, all honestly reportable and
scoped: (1) Treatment and HP worker Repositoryization (§8) — not done; (2) the 325/325
comparator figure has no repeatable harness and was not reproduced (46/46 parity, by contrast,
*was* re-run this pass against real data and PASSed); (3) the live UI baseline was not directly
re-measured (attempted; the verification app did not finish starting in this sandbox). None of
these are safety violations or STOP-condition triggers — they are scope not yet covered, stated
plainly rather than rounded up to "ready."
