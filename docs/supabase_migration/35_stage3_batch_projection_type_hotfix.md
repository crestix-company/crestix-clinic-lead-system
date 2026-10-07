# Step3 Batch Projection PostgreSQL Type Safety Hotfix

## Scope

The Step3 batch projection `UPDATE ... FROM (VALUES ...)` allowed PostgreSQL to
infer some VALUES columns as text. In particular, the nullable `age_probability`
value was inferred incompatibly with `public.clinics.age_probability`
(`double precision`). This hotfix types every bound projection value explicitly;
it does not change Maps matching, projection rules, or production clinic data.

## Implementation

`src/repository/supabase_write_adapter.py` now defines the fixed
`POSTGRES_PROJECTION_TYPES` map and applies a static cast to every placeholder
in the batch VALUES source. Data remains parameterized. Numeric values are
normalized to `float | None` and `int`; nullable booleans remain `None` or bool.
The three projection arrays remain psycopg `Jsonb` values. `effective_json`
remains TEXT, including legacy literal `\\u0000` compatibility. No dynamic SQL
type names or data values are interpolated.

## Verification

- Focused repository tests: 81 passed.
- Full pytest: 1116 passed, 0 failed, 25 skipped; critical runtime skips: 0.
- The 25 skips are existing artifact-dependent MHLW/Phase4 tests whose local
  generated databases/outputs are absent; they are not runtime-path skips.
- Production SQLite was used only as a read-only source for a temporary
  comparator snapshot; the production file was not modified.

### Live `clinic_runtime` rollback canaries

Each size exercised the full matched import path, including the typed batch
projection UPDATE. The repository's commit call was intercepted and performed
an actual rollback. Reconciliation after every size found no canary result,
batch marker, clinic sentinel, or history row and no business-row mutation.

| Rows | Elapsed | Rows/sec | SQL executions | Projection prefetch | Projection calculation | Projection update | Rollback |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 10 | 0.322 s | 31.1 | 10 | 0.053 s | 0.006 s | 0.033 s | 0.015 s |
| 100 | 0.850 s | 117.7 | 10 | 0.050 s | 0.019 s | 0.062 s | 0.015 s |
| 1,000 | 2.923 s | 342.1 | 16 | 0.110 s | 0.092 s | 0.700 s | 0.015 s |

The 1,000-row linear projection is approximately 50.7 seconds for 17,355 rows;
with the required 20% safety margin it is approximately 60.9 seconds. This is a
projection from the live rollback canary, not a run of the real CSV.

PostgreSQL sequences are non-transactional. As expected, the rollback-only
canaries consumed sequence values even though their data rows rolled back. The
post-canary states observed were `provenance.change_history_id_seq = 210491`
and `provenance.google_maps_results_id_seq = 46775`. These values were not
reset; no corresponding benchmark rows remain.

### Final live integrity

- `current_user = clinic_runtime`; LOGIN true, SUPERUSER/CREATEDB/CREATEROLE/
  REPLICATION/BYPASSRLS false.
- `public.clinics = 162258`.
- `provenance.google_maps_results = 15339`.
- Failed real batch `8d16468f07ea93faa216010d963e2decc130f2b9b15df3ff89880e537853fc5e`
  remains absent.
- UUID duplicate groups = 0; medical_key duplicate groups = 0; canonical
  clinic_id duplicate groups = 0.
- Other active `clinic_runtime` transactions = 0.
- `idx_clinics_active_base_clinic_id` remains valid and unchanged.
- Unexpected business-row mutation = 0.

## Decision

The PostgreSQL type-inference failure is covered by regression tests and the
full live batch projection path passed 10-, 100-, and 1,000-row rollback
canaries. RETRY REAL 17,355 ROW STEP3 = YES. The real Collector CSV was not run
by this hotfix.
