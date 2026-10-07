# Stage3 Google Maps Full Write-Path Optimization

Date: 2026-10-07

## Scope and safety

This change optimizes only `SupabaseProvenanceWriteRepository.import_maps_results()`.
The target-matching algorithm and the live `idx_clinics_active_base_clinic_id` index were
left unchanged. The real 17,355-row Collector CSV was not run. Production verification used
read-only SQL; the full-path benchmarks used the isolated synthetic cursor in
`scripts/supabase_migration/stage3_full_write_benchmark.py`, which has no database connection.

## Write path

For the matched clinic IDs, the repository now obtains `base_json`, UUID, and `medical_key` in
one deterministically ordered `FOR UPDATE` query. It applies `build_maps_update()` to in-memory
clinic state in original CSV row order. This keeps last-row behavior and confirmed-website
anti-downgrade semantics while preserving one history entry for each sequential state change.

Google Maps result rows, final clinic `base_json`, and `change_history` are sent in bounded
multi-row `VALUES` statements. Research results and manual overrides are prefetched in bulk;
the unchanged `refresh_clinic_projection_preserving_identity()` Python function calculates
each affected clinic's projection once, and the exact projection whitelist is bulk-updated.
`effective_json` remains TEXT. The batch marker remains last, and there is one commit for the
whole batch; any exception rolls back and is re-raised. Retry dedupe by the same batch hash is
unchanged.

## Equivalence and rollback coverage

The repeated-clinic regression applies two rows in input order and compares final `base_json`,
each intermediate history before/after snapshot, and every projected column with the canonical
sequential `build_maps_update()` plus projection functions. It also checks that UUID/PK are not
projection update targets. Existing website-preservation, dedupe, matcher-priority, phone
ambiguity, not-found, and NUL compatibility tests remain in the focused suite.

Injected failures during Maps bulk insert, base JSON update, and projection calculation each
assert one rollback and zero commits. There are no intermediate commits.

## Isolated full-path benchmark

Synthetic clinics are unique per row, so each size exercises matching, clinic-state locking,
Maps result serialization, base JSON update, one history row, bulk dependency prefetch, Python
projection, and projection update. The fake executor records SQL statement executions and
does not persist rows.

| Rows | Elapsed | Rows/sec | SQL executions |
|---:|---:|---:|---:|
| 100 | 0.006 s | 15,566 | 10 |
| 1,000 | 0.044 s | 22,847 | 16 |
| 10,000 | 0.467 s | 21,423 | 100 |
| 17,355 synthetic projection | 0.857 s | 20,262 | 169 |

At 17,355 synthetic rows, measured phases were: validation 0.105 s; target prefetch 0.048 s;
matching 0.024 s; clinic-state prefetch 0.004 s; Maps calculation 0.178 s; Maps result bulk
insert serialization/execution 0.004 s; base write 0.002 s; history 0.006 s; projection
prefetch below 0.001 s in the empty-dependency fixture; projection calculation 0.387 s;
projection bulk write 0.013 s; fake commit below timer resolution. The 10,000-row fixture
also passed the <5 minute limit.

The 17,355 figure is an isolated code-path run, not a live PostgreSQL write benchmark. A
read-only Production `SELECT 1` calibration had a 14.41 ms median, 19.27 ms p95, and 115.89 ms
maximum over 50 executions. At the measured p95, 169 statement round trips would be about
3.3 seconds of network/execution latency; this does not include actual bulk-write, lock,
trigger, WAL, or commit costs. Allowing a deliberately large 1 second average per SQL execution
still gives under 3 minutes including the isolated Python work, so the modeled full path is
under the 10-minute projection gate. The acceptance duration must nevertheless be measured
when the user manually retries the real CSV; this estimate is not a substitute for that
production acceptance run.

For comparison, the prior path issued roughly eight statements per matched row when each row
changed clinic state (result insert, clinic read/update, history insert, and four projection
statements): approximately 138,841 statements at 17,355 rows. The new path executes 169
statements at that size in the synthetic unique-clinic case. SQL statement count grows by
bounded chunk count, not one execution per clinic/CSV row.

## Production read-only reconciliation

At verification time, before any import retry:

- `public.clinics`: 162,258
- `provenance.google_maps_results`: 15,339
- failed batch `8d16468f07ea93faa216010d963e2decc130f2b9b15df3ff89880e537853fc5e`: absent
- UUID duplicate groups: 0
- `medical_key` duplicate groups: 0
- active canonical `clinic_id` duplicate groups: 0
- other active `clinic_runtime` transactions: 0
- existing clinic ID expression index: valid; `EXPLAIN` uses its Index Scan
- runtime role flags: `SUPERUSER=false`, `BYPASSRLS=false`, `CREATEDB=false`, `CREATEROLE=false`

No Production business mutation was performed. The production index was neither created nor
modified by this work.

## Regression status and retry gate

The focused Stage4-D repository suite passed 80 tests. The final offline full-suite run, with
the runtime URL unset so live filter-regression fixtures do not attempt to open the unavailable
local SQLite comparison DB, completed with 1,090 passed, 50 skipped, and 0 failures. The 50
skips are the existing 25 artifact/data-dependent tests plus 25 live filter comparisons that
require both Production Supabase and the unavailable local SQLite comparison database. A run
with the runtime URL present reached those comparisons but failed at setup on the missing local
SQLite path; no database file was created or rebuilt.

The matching/index gate remains satisfied. The full write-path implementation is ready for
review. The actual import still must be manually timed and reconciled before acceptance. The
runtime credential was exposed in a local tool output during environment inspection; rotate it
before the next runtime session. No credential was added to repository files.
