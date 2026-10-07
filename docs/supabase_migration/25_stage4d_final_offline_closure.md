# Stage4-D Gate2 — Final Offline Closure

Date: 2026-10-07. Branch `feature/supabase-shadow-migration`. Starting HEAD `ff5e81b`. No Live
Supabase DDL/DML executed in this pass.

## 1. Treatment worker (`scripts/research_worker.py`) — read in full, Repositoryized

Read all 729 lines (previously unread). It is a standalone CLI (`python -m scripts.research_worker
start/resume/status/stop`), deliberately independent of app_v2.py, using **three separate
SQLite databases**:

- `cache.sqlite3`, `progress.sqlite3` (per-manifest, gitignored, local checkpoint/resume/dedup
  state) — **not** part of the 19-table Supabase migration (confirmed against Gate1.5's own
  prior classification: "operational state outside the 19-table target"). Not Repositoryized;
  out of scope by design, same as before.
- `treatment_research_final.sqlite3` (shared, `TREATMENT_RESEARCH_DB_PATH`) — **the only one in
  scope**, mapping to `treatment.clinic_research_status`/`treatment.clinic_treatment_research`.

All persistent writes to the shared DB were already concentrated in one function,
`write_clinic_atomic()` (DELETE+INSERT on DONE, `ON CONFLICT DO UPDATE` upsert for status,
always, one transaction) — an already-clean seam. New files:
`src/repository/treatment_write_contracts.py` (Protocol), `treatment_sqlite_write_adapter.py`
(thin delegate to the unmodified `write_clinic_atomic`), `treatment_supabase_write_adapter.py`
(Postgres port, same semantics). `scripts/research_worker.py`'s `process()` now calls
`treatment_repo.write_clinic_result(...)` instead of `write_clinic_atomic()` directly;
`run_worker()` builds the repo via a new `_build_treatment_repo()` selector
(`CLINIC_WRITE_BACKEND`, default `sqlite`, consistent with app_v2.py's own selector).
`cache_clinic_pages()`/the two local DBs are untouched. All 53 existing
`tests/test_research_worker.py` tests still pass (they call `write_clinic_atomic` directly,
unaffected). New tests added: SQLite delegate parity, idempotent same-clinic-twice, retry-after-
success-no-duplicate, worker-restart-preserves-prior-DONE-result, Supabase SQL-shape
(DELETE+INSERT+ON CONFLICT), Supabase rollback-not-fallback-on-failure.

## 2. HP worker (`src/master/hp_research_batch.py`) — Repositoryized

Single target table (`hp_research_batch_results` → `hp_research.clinic_hp_research`), single
write function (`upsert_result()`, already idempotent `ON CONFLICT(clinic_id) DO UPDATE`). New
files: `hp_write_contracts.py`, `hp_sqlite_write_adapter.py` (thin delegate),
`hp_supabase_write_adapter.py` (Postgres port). Two things the Postgres port had to get right,
not guess at:
- **Column rename**: SQLite's `hp_abc_candidate` is `machine_hp_rank` in Postgres (per
  `schema_target.sql`'s own comment: "旧 hp_abc_candidate"). Confirmed and applied.
- **`portal_name`**: a Postgres-only column with no SQLite equivalent ("ETL投入時にURLから確定"
  per schema comment — a one-time migration-time derivation). For ongoing live writes, computed
  at write time via the existing `portal_name_for_url()` pure function from
  `src.master.hp_site_type` — reused, not reimplemented.

`run_batch()` now calls `hp_repo.upsert_result(result)` via a new `_build_hp_repo()` selector
(same env var/default pattern). All 16 existing HP-related tests still pass. New tests added:
SQLite delegate parity, idempotent-same-item-twice-tracks-attempts, Supabase SQL-shape (rename
+ JSONB wrapping + ON CONFLICT), Supabase rollback-not-fallback.

## 3. Global A+B direct SQLite runtime WRITE audit — result

Re-grepped every A/B file at the business/worker call-site layer after both workers were
rewired:

| File | Direct SQLite WRITE at call-site layer |
|---|---|
| `app_v2.py` | 0 |
| `src/master/jobs.py` (excl. Class C `repair_reset_job_items`) | 0 |
| `src/enrichment/search_provider.py` | 0 |
| `scripts/research_worker.py`'s `process()` | 0 (only the `write_clinic_atomic` *definition* remains, as the adapter-layer implementation) |
| `src/master/hp_research_batch.py`'s `run_batch()` | 0 (only the `upsert_result` *definition* remains, same reasoning) |

**Global A+B direct SQLite runtime WRITE = 0 at the business/worker call-site layer — now
measured across the complete Gate1.5 A+B inventory, not just the main app.** The underlying
adapter-layer implementations (`store.py`, `google_maps.py`, `write_clinic_atomic`,
`upsert_result`) retain their SQL by design, consistently classified as the SQLite Adapter
implementation layer the Repository calls into — the same relationship as the pre-existing
READ-side architecture (`sqlite_adapter.py` → `ClinicStore`).

## 4. Idempotency / unknown commit outcome contract

No new ID scheme was introduced (per the Owner's own instruction: "新しいIDを無意味に増やさない").
The pattern already present across every write path IS the contract:

| Surface | Stable identity | Mechanism | Re-submission effect |
|---|---|---|---|
| jobs (`research_job_items`) | `(job_id, clinic_id)` composite PK | claim via `UPDATE ... WHERE state='PENDING'` (no-op if already claimed); `finish_item`/`requeue_item_for_budget_or_pause` are plain `UPDATE`s on the existing row | Re-running `finish_item`/`requeue_...` with the same arguments overwrites the same row with the same values — no duplicate, no corruption. Proven by existing `test_crash_recovery_returns_running_item_to_pending`, `test_job_budget_pause_resume_done_not_researched`. |
| search cache/usage | `query_key` (content hash) | `ON CONFLICT(query_key) DO UPDATE` | Re-submitting the same query's result is a no-op overwrite. `search_usage` rows are append-only attempts, not results — an extra attempt row under ambiguous commit is the same "charged, not refunded" behavior the original SQLite code already had (documented, unchanged). |
| Treatment (`clinic_treatment_research`/`clinic_research_status`) | `clinic_id` (+ `treatment_category_name` for the detail table) | full DELETE+INSERT replace on DONE; `ON CONFLICT(clinic_id) DO UPDATE` on status, always | Proven by new `test_sqlite_treatment_repo_retry_after_external_success_no_duplicate` and `..._worker_restart_resumable_state_unaffected`. |
| HP batch (`clinic_hp_research`) | `clinic_id` | `ON CONFLICT(clinic_id) DO UPDATE`, `attempts = prior_attempts + 1` | Proven by new `test_sqlite_hp_repo_idempotent_same_item_twice_tracks_attempts`. |
| `save_research`/`override` | `clinic_id` (+ `field` for overrides) | JSON merge then `ON CONFLICT`/`INSERT OR REPLACE` | Merging the same incoming JSON twice is idempotent (dict update with identical values). |

**"Commit succeeded but the client never received the ack" case**: for every surface above, a
retry is *by construction* indistinguishable from the original call once it reaches the DB — the
retry re-applies the same identity key with the same payload, landing on the same row. No
distributed-transaction/outbox pattern was needed because none of these paths cross two
different databases in one logical operation (each write path commits once, to one database)
except the already-documented two-phase `CachedSearch` split, which is intentionally
"charged, never refunded" on ambiguous outcome (pre-existing, unchanged behavior, now also true
on the Supabase port).

## 5. UI baseline — directly measured, not Streamlit-dependent, ALL MATCH

New script `scripts/supabase_migration/stage4d_ui_baseline.py`: calls
`src.repository.backend.build_repositories()` directly (the real, unmodified READ path) with
the exact `Filters` construction `app_v2.py:simple_sales_ui()` builds at its default widget
state (verified by reading that function's source, not guessed). No Streamlit process, no
browser. Run against real Production Supabase data:

```
営業対象: expected=997 actual=997 MATCH
Comdesk出力対象: expected=997 actual=997 MATCH
UUIDあり: expected=515 actual=515 MATCH
UUIDなし: expected=482 actual=482 MATCH
眼科: expected=68 actual=68 MATCH
keywordクリニック: expected=747 actual=747 MATCH
Treatment: PASS
pagination_page2: PASS
HP_A_leq_AB: PASS
ALL MATCH: True
```

This closes the gap from the previous pass (live-browser verification had stalled in sandbox).

## 6. Comparator harness — new, reproducible, found and root-caused a real (pre-existing, not
   caused by this pass) discrepancy

No script reproducing the historical "325/325 shadow/comparator" figure was found anywhere in
`scripts/` or `tests/` — it reads as a one-time count from the live async comparator in
`src/repository/cutover.py` (`logs/read_cutover.log`), not a repeatable harness. Built a new one,
`scripts/supabase_migration/stage4d_read_comparator.py` (read-only against both backends, same
discipline as `parity_harness.py`), covering 38 cases: counts across 5 prefectures, 5
departments, 5 keywords, both scopes, all uuid_modes, all effective_rank combinations; 5 pages
of pagination ID-for-ID; funnel; metrics; and per-row detail/Treatment/HP-rank/UUID/medical_key
lookups for a 20-row sample. Run against real production data:

```
cases = 38
matched = 37
mismatch = 1
[MISMATCH] funnel: sqlite=[('全マスター', 162258), ('重複確認待ちを除く', 162258),
  ('病院・センター除外（営業対象外）', 153077), ('現存クリニック（一覧基準日）', 111545),
  ('HP ABC判定', 997), ('都道府県', 997), ('医科・歯科', 997), ('最終営業対象', 997)]
 supabase=[('全マスター', 162258), ('重複確認待ちを除く', 162258),
  ('病院・センター除外（営業対象外）', 153077), ('現存クリニック（一覧基準日）', 111545),
  ('都道府県', 23433), ('医科・歯科', 13186), ('HP ABC判定', 997), ('最終営業対象', 997)]
```

**Root cause, found and verified, not hand-waved**: `src/master/filters.py:clauses()` (SQLite)
iterates `[ranks, effective_ranks, prefectures, medical_types, hot]` in that order when building
funnel steps. `src/repository/supabase_filters.py:clauses()` iterates
`[ranks, prefectures, medical_types, hot]` in its shared loop and handles `effective_ranks`
**separately, afterward** (necessarily — it requires a JOIN-based SQL expression the simple
`col IN (...)` loop can't express, see that function's own code). The two backends therefore
report funnel steps in a **different order** whenever both `effective_ranks` and
`prefectures`/`medical_types` are set together (the harness's `BASE` filter does; the existing
`parity_harness.py` scenario `44_funnel_labels_and_counts` uses `Filters(active_only=False,
hp_only=False)` — no ranks/prefectures/medical_types set at all — so it never exercises this
path, which is why Stage4-C's 46/46 never caught it).

**Business impact: none found.** The final `最終営業対象` value is identical (997) on both
backends in every case checked — this is a cosmetic-only divergence in the *intermediate*
funnel breakdown's step order/labels, not the end count the UI's `営業対象`/`Comdesk出力対象`
metrics are built from (those call `count()`, not `funnel()`, and `count()`'s mismatch-free
across all 38 cases confirms the actual filtering logic itself agrees).

**Not fixed in this pass, by design — flagged for an explicit decision, not silently patched**:
`src/repository/supabase_filters.py` is a live, heavily-optimized READ-path file (the module
docstring documents a measured 4.7x query-plan optimization around this exact function) that
this entire Gate2 WRITE-cutover effort deliberately never touched. Reordering it to match
SQLite's step order is almost certainly a small, low-risk fix (move the `effective_ranks` block
earlier), but it is a change to the live READ Primary path, outside this pass's WRITE-focused
mandate, and is reported here rather than patched unreviewed at this hour.

## 7. Parity / regression — re-confirmed

- `parity_harness.py`: 46/46 PASS, re-run against real data after the Treatment/HP changes.
- Full `pytest`: **1081 passed, 25 skipped, 0 failed** (up from 1070 — 11 new tests this pass:
  7 Treatment + 4 HP Repository tests). Critical runtime skip count unchanged at 0.
- `medical_key` regression subset: 10/10 PASS, re-run after this pass's changes.
- WRITE backend selector regression: 3/3 PASS.
- Silent-fallback-rolls-back-not-falls-back regression: 4/4 PASS (now covering Treatment and HP
  adapters too, not just the main Clinic/Settings/Jobs/Search adapters).

## 8. Migration SQL / runtime role — re-reviewed, no changes needed

`scripts/supabase_migration/stage4d_runtime_write_schema.sql` already granted
`SELECT, INSERT, UPDATE, DELETE` on `treatment.clinic_research_status`/
`treatment.clinic_treatment_research` and `SELECT, INSERT, UPDATE` on
`hp_research.clinic_hp_research` to `clinic_runtime`, with RLS policies for all three, from the
previous pass (the full Gate1.5 mandatory-table list was already anticipated). Re-checked against
the exact SQL the two new Supabase adapters issue (`DELETE`+`INSERT` for Treatment, `SELECT`+
`INSERT ... ON CONFLICT` for HP) — **no additional grant, sequence, or policy is required.**
Still a draft file, still unapplied.

## 9. Production safety — confirmed at the end of this pass

- Production SQLite SHA-256: unchanged, all 3 files, re-verified.
- `public.clinics` = 162,258 (re-verified, read-only, after the comparator/baseline scripts ran
  against live Supabase).
- Supabase row data writes this pass: **0**. Live DDL executed: **0**.
- `git status --short`: only this pass's files modified/new.
- `CLINIC_WRITE_BACKEND` default: still `sqlite`.

## 10. LIVE CUTOVER READY verdict

| Requirement | Status |
|---|---|
| Gate1.5 PASS | ✅ |
| Gate2 Repository complete | ✅ (app_v2.py, jobs.py, search_provider.py, google_maps.py, Treatment worker, HP worker — all Repositoryized) |
| import_maps_results complete | ✅ |
| Treatment worker Repositoryized | ✅ |
| HP worker Repositoryized | ✅ |
| jobs idempotency complete | ✅ |
| Treatment idempotency complete | ✅ |
| HP idempotency complete | ✅ |
| Global A+B direct SQLite runtime WRITE = 0 | ✅ measured across the complete A+B inventory |
| medical_key PASS | ✅ |
| high-range ID PASS | ✅ |
| migration SQL READY | ✅ (unapplied) |
| runtime role/RLS/sequence SQL READY | ✅ (unapplied, re-verified sufficient for Treatment/HP) |
| pytest failure = 0 | ✅ (1081 passed) |
| critical runtime skip = 0 | ✅ |
| 46/46 parity PASS | ✅ re-confirmed |
| reproducible READ comparator mismatch = 0 | ❌ **1 mismatch found** (funnel step order, cosmetic, final counts unaffected, root-caused, not fixed — see §6) |
| UI baseline (997/997/515/482/68/747) | ✅ all 6 exact matches, directly measured |
| Treatment / HP / pagination PASS | ✅ |
| Production SHA unchanged | ✅ |
| Supabase counts unchanged | ✅ |
| Supabase row write = 0 | ✅ |
| Live DDL = 0 | ✅ |
| WRITE Primary = SQLite | ✅ |

**Verdict at the end of the previous pass: LIVE CUTOVER READY = NO — one item away** (the
funnel ordering mismatch above). See §11 below for the Owner Decision closing it.

## 11. Owner Decision — funnel ordering fixed, comparator mismatch closed to 0

The owner reviewed §6/§10 and decided: SQLite's existing funnel step order is canonical
behavior; Supabase must be changed to match it (not the reverse), minimal-scope only.

**Root cause reconfirmed before touching any code** (per the owner's own instruction to verify,
not just trust the prior write-up): `src/master/filters.py:clauses()` has ONE shared loop over
`[ranks, effective_ranks, prefectures, medical_types, hot]` because every one of those fields
reduces to a simple `col IN (...)` SQL fragment in SQLite. `src/repository/supabase_filters.py:clauses()`
could not put `effective_ranks` in its equivalent loop because the Postgres translation of
`effective_hp_rank()` requires a JOIN-based SQL expression (correlated `EXISTS` against
`hp_research.clinic_hp_research`) that doesn't fit a `col IN (...)` template — so it was
previously handled in a separate `if` block placed AFTER the loop (which covered `ranks`,
`prefectures`, `medical_types`, `hot`), producing step order `[ranks, prefectures,
medical_types, hot, effective_ranks, ...]` instead of SQLite's `[ranks, effective_ranks,
prefectures, medical_types, hot, ...]` whenever `effective_ranks` was combined with
`prefectures` and/or `medical_types`.

**Fix applied, minimal scope only** (`src/repository/supabase_filters.py`, `clauses()`):
split the single loop into three parts in canonical order — (1) `ranks` alone, (2)
`effective_ranks` (the existing JOIN-based block, body byte-for-byte unchanged, only its
position in the function moved earlier), (3) a loop over the remaining `[prefectures,
medical_types, hot]`. **Not changed**: any WHERE-condition SQL text, any predicate semantics,
the hospital/center exclusion clause, site_types/departments/treatments/signals handling,
`count()`/`query()`/`get()` logic, the SQLite implementation, `medical_key`, any Write
Repository/adapter file, the migration SQL, the runtime role SQL, or any ID-generation logic —
confirmed by `git diff` touching only this one function's statement order in one file.

**Regression tests added** (`tests/test_supabase_filters_regression.py`, real production data,
auto-skipped without `SUPABASE_DB_URL`): `test_funnel_step_order_matches_sqlite` and
`test_funnel_final_count_matches_sqlite`, parametrized over exactly the three combinations the
owner specified (`effective_ranks+prefectures`, `effective_ranks+medical_types`,
`effective_ranks+prefectures+medical_types`) — asserting both the full `(label, count)` step
list and the final count are identical to SQLite. All 6 new parametrized cases PASS.

**Full re-verification after the fix** (all against real production/Supabase data):

- `scripts/supabase_migration/stage4d_read_comparator.py`: **cases = 38, matched = 38,
  mismatch = 0** (was 37/38 before the fix).
- `scripts/supabase_migration/parity_harness.py`: 46/46 PASS, re-run.
- `scripts/supabase_migration/stage4d_ui_baseline.py`: all 6 values still exact matches
  (営業対象=997, Comdesk=997, UUIDあり=515, UUIDなし=482, 眼科=68, keyword=747); Treatment/
  pagination/HP sanity checks PASS.
- Full `pytest`: **1087 passed, 25 skipped, 0 failed** (up from 1081 — the 6 new funnel-order
  tests). Critical runtime skip count still 0.
- `tests/test_supabase_filters_regression.py` in full: 25/25 PASS (19 pre-existing + 6 new).
- Production SQLite SHA-256: unchanged, all 3 files, re-verified after the fix.
- Supabase `public.clinics` = 162,258, 19-table total = 632,903 — re-verified, unchanged.
- Supabase row writes this pass: 0. Live DDL: 0. `CLINIC_WRITE_BACKEND`: still `sqlite` default.

## 12. LIVE CUTOVER READY — final verdict

Every item in the Owner's Definition (their "Final Offline Fix" message, §14) is now satisfied:
Gate1.5 PASS; Repository coverage complete (app_v2.py/jobs.py/search_provider.py/
google_maps.py/Treatment worker/HP worker); import_maps_results complete; jobs/Treatment/HP
idempotency complete; Global A+B direct SQLite runtime WRITE = 0; medical_key PASS; high-range
ID PASS; migration SQL READY (unapplied); runtime role SQL READY (unapplied); pytest failure =
0; critical runtime skips = 0; 46/46 parity; **reproducible comparator mismatch = 0**; UI
baseline exact (all 6); Treatment/HP/pagination PASS; Production SHA unchanged; Supabase counts
unchanged; Supabase row writes = 0; Live DDL = 0; WRITE Primary = SQLite.

# LIVE CUTOVER READY = YES

This is a design/implementation-readiness judgment, not a record of anything having been
applied to the live Supabase project — no DDL, role, grant, policy, sequence, or data write was
executed in this pass or any prior Gate2 pass. It means: the WRITE Repository/Adapter layer,
the identity contracts, the ID-collision guard, the migration SQL, and the offline-reproducible
verification suite are all complete, tested, and internally consistent, and are ready for the
owner to walk through the live gates (DDL apply → Security Advisor → Rollback Canary → Failure
Injection → Persistent Canary → Reconciliation → WRITE Cutover) in person.
