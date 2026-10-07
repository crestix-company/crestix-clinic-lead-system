# Step4 Dynamic HP Research Targets

## Eligibility change

The former force-off rule used `public.clinics.hp_status='UNRESEARCHED'`. That
column is a display/projection field and is not a record of HP research
completion. Step4 now uses `hp_research.clinic_hp_research` as its completion
source: force OFF requires that no row exists for the clinic, regardless of
whether an existing row records `OK`, `ERROR`, or `INVALID_URL`. Force ON omits
the anti-existence condition and permits researched clinics to be selected.

Both `maps_hp_available_count()` and `maps_hp_candidate_ids()` call the same
predicate builder. Their common conditions are: not merged, `merge_hold=false`,
active, Maps status `MAPS_MATCHED_WEBSITE`, and a trimmed nonblank Maps website;
optional prefecture and medical type filters are shared as well. The UI count
and created HP job both use these runtime methods. The SQLite projection cannot
establish completion from the Supabase ledger, so the UI reports zero targets
and job creation is rejected when the runtime is not Supabase.

The existing screen metrics (`Webサイト調査完了`, `Webサイト調査失敗`,
`Webサイト未調査`) were not changed; they continue to derive from
`hp_research.clinic_hp_research`.

## A/B clinic_id set audit

Audit was read-only using `clinic_runtime`. `A_global` is all clinics with a
nonblank `maps_website_url` and no row in `hp_research.clinic_hp_research`.
`B_current` is the actual Step4 candidate API for 東京都 × 医科, force OFF.
`A_scope` applies that same prefecture, medical type, Maps-matched status,
nonblank Maps URL, and no-research definition, while leaving active/merge/hold
gates for comparison. `B_scope` is the actual Step4 candidate set under the
same UI scope.

| Set comparison | A count | B count | A only | B only | Both |
|---|---:|---:|---:|---:|---:|
| Global A vs current-filter B | 10,347 | 9 | 10,338 | 0 | 9 |
| Same-scope A vs B | 9 | 9 | 0 | 0 | 9 |

The global A breakdown matches the supplied baseline: Tokyo medical 9, Tokyo
dental 773, other-prefecture medical 6,050, other-prefecture dental 3,513,
other/blank type 2. Same-scope A-only reason buckets are all zero because
`A_scope only = 0`; B-scope-only reason buckets are all zero because
`B_scope only = 0`.

Before the fix, the old default count was 9,493, producing 9,484 extra targets
in the Tokyo × medical comparison. After the fix, count and candidate IDs are
both 9, and the clinic_id sets match exactly.

## Tests and caveat

- Focused Step4/runtime UI tests: 11 passed.
- Full pytest: 1,125 passed, 1 failed, 25 skipped.
- The single failure is the existing `test_metrics_aggregation_matches_sqlite` live comparator. It compares
  archived Production SQLite to current Supabase data and differs only in
  Google Maps metrics (SQLite/Supabase: Maps not found 49/419, Maps website
  acquired 10,311/20,658, Maps presence confirmed 11,785/25,860). The change
  does not modify metrics or these data paths; the production SQLite snapshot
  was read-only. The 25 skips are existing generated-artifact-dependent
  MHLW/Phase4 tests.
- No Production database or SQLite mutation was performed.

## Decision

Step4 target consistency = PASS. The live count and candidate APIs return the
same nine clinic IDs for the default Tokyo × medical scope. A 50-item limit
will therefore yield the nine currently eligible targets, not 50. The
50-clinic canary may proceed only with that actual selected count understood;
the real count remains dynamic and is not hardcoded.
