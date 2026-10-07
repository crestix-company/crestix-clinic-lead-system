# Stage3 Google Maps Step3 Performance Hotfix

Date: 2026-10-07

## Scope and safety

This hotfix changes only Supabase Google Maps clinic target matching and adds a read-only
matcher benchmark. The interrupted 17,355-row CSV was not rerun. No Production business rows
were written. The live expression index was already present before the final benchmark and was
not recreated.

## Root cause and matching behavior

The old import loop called `_find_target()` for every CSV record. Official identity resolution
issued one full-table lookup against the empty `base_json->>'medical_institution_number'` field
and another lookup against `base_json->>'clinic_id'`. The latter used a sequential scan before
the expression index existed. That is the confirmed N+1 scan bottleneck.

The importer now calls `_prefetch_maps_targets()` once per transaction and resolves each input
with `_find_target_prefetched()` in memory. It bulk-loads active internal IDs/canonical clinic
IDs, the legacy official-number field, phone candidates, and normalized name/address pairs.
Effective JSON is bulk-loaded only for duplicate-phone candidates that need the historical score
comparison. A per-row SQL query is not issued for identity matching.

Priority is unchanged: internal ID, official identifier (`medical_institution_number` then
canonical `clinic_id`), phone, name/address, then AMBIGUOUS/NOT_FOUND. Phone scoring, reverse
sort tie handling, and the 30-point gap are unchanged. Import remains one transaction with
batch dedupe, raw provenance, website anti-downgrade, history and projection behavior intact.

## Index migration and plan

Repository convention stores operational Supabase SQL under `scripts/supabase_migration/`;
there is no tracked `supabase/migrations/` tree. The index definition is recorded in
`scripts/supabase_migration/stage3_google_maps_clinic_id_index.sql`.

Before apply, read-only checks returned: clinics 162258; canonical clinic IDs 162242, duplicate
groups 0; UUID duplicate groups 0; medical_key duplicate groups 0; failed batch rows 0. The
index was then created concurrently once. The live catalog reports it valid and ready.

Before index: `Seq Scan`, about 1091 ms in the user's recorded plan (the earlier plan captured
in the Codex session cost estimate showed a sequential scan as well).

After index, `EXPLAIN (ANALYZE, BUFFERS)` for the indexed clinic_id predicate:

```text
Index Scan using idx_clinics_active_base_clinic_id
actual rows=1, loops=1
Buffers: shared hit=3 read=1
Execution Time: 0.770 ms
```

The actual bulk candidate predicate also used `BitmapOr` with both `clinics_pkey` and
`idx_clinics_active_base_clinic_id`; it completed in 2.126 ms for one representative identity.

## Read-only matcher benchmark

`scripts/supabase_migration/stage3_google_maps_match_benchmark.py` samples representative
canonical clinic IDs and runs only candidate prefetch plus in-memory resolution as
`clinic_runtime`. It performs SELECTs only and does not call the import or projection code.

| Rows | SQL round trips | Matched | Unmatched | Ambiguous | Prefetch + match | Rows/sec |
|---:|---:|---:|---:|---:|---:|---:|
| 100 | 2 | 100 | 0 | 0 | 0.859 s | 116 |
| 1,000 | 2 | 1,000 | 0 | 0 | 0.609 s | 1,643 |
| 10,000 | 2 | 10,000 | 0 | 0 | 0.923 s | 10,837 |

The corresponding in-memory matching times were 0.00017 s, 0.00133 s, and 0.00820 s.

These identity-only synthetic records exercise the real canonical IDs but do not reproduce the
CSV's full phone/name fallback distribution. The benchmark records the legacy official-number
compatibility preload as one bulk query; Production currently has zero nonblank values in that
field. The 17,355-row matcher-only estimate is approximately 1.6 seconds at the 10,000-row
measurement rate. This estimate is not a full import estimate.

Before optimization the actual run exceeded two hours and was manually interrupted. No completed
row count was preserved, so an exact before rows/sec cannot be calculated. The operation made no
partial commit. At the total-file denominator only, elapsed throughput was below 2.41 rows/sec.

## Projection profile and full-import estimate

Projection was profiled with three read-only SELECTs used by `_refresh_projection_tx()` for 100
clinics: 300 SELECT round trips took 4.809 seconds, or 16.03 ms per round trip. No row locks,
updates, or writes were performed. The three projection SELECTs alone extrapolate to about 834.6
seconds (13.9 minutes) for 17,355 matched clinics.

The current import also performs per matched clinic a raw Maps result INSERT, a clinic row
SELECT FOR UPDATE, a base_json UPDATE, and a projection UPDATE; change_history may add another
INSERT when the projection input changes. At the measured SELECT round-trip rate, an approximate
full-import model is 32 minutes without history inserts and about 41 minutes if each match
records history. This is a network round-trip estimate, not a production DML benchmark. The
projection path is therefore the next bottleneck and the 10-minute gate is not met.

For an identity-heavy 17,355-row batch, the current normal path is approximately 121,500 SQL
round trips before optional history and duplicate-phone detail fetches: roughly 7 per matched
row (Maps insert, clinic select/update, three projection reads, projection update), plus a few
batch-level prefetch/dedupe statements. The old matcher alone issued at least two identity SQL
lookups per row, 34,710 round trips, before phone/name fallback queries.

## Regression and live reconciliation

- Focused repository suite: 76 passed.
- Full pytest: 1111 passed, 0 failed, 25 skipped. The skips are legacy MHLW/sidecar fixtures
  requiring local artifacts not present in this checkout; no critical runtime skip occurred.
- Regression coverage includes escaped-NUL compatibility, internal-ID priority, official ID,
  phone fallback, duplicate-phone tie/score behavior, name/address, NOT_FOUND, website preserve,
  batch dedupe, rollback and identity contract.
- Static inspection confirms import calls batch prefetch once; `_find_target_prefetched()` has
  no SQL calls. The per-row `base_json->>'medical_institution_number'` and
  `base_json->>'clinic_id'` lookups are absent from the import loop.
- Final read-only Production check: clinics 162258; google_maps_results 15339; UUID duplicate
  groups 0; medical_key duplicate groups 0; canonical clinic_id duplicate groups 0; failed batch
  rows 0; other active clinic_runtime transactions 0; index valid/ready = true.
- Unexpected Production mutation: 0.

Production SQLite SHA-256 values were rechecked and remained equal to the recorded Clinics,
Treatment, and HP baselines.

## Gate result

Matching semantics are unchanged and matcher performance is substantially improved. The
full-import estimate exceeds the 10-minute acceptance limit because projection and per-row
write round trips remain. Projection behavior was not rewritten in this hotfix.

**RETRY REAL 17,355 ROW STEP3 = NO**

The next approved work should profile and reduce projection/write round trips while preserving
the single-transaction rollback and projection semantics, then rerun the acceptance benchmark.
