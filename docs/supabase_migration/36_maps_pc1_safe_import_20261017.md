# PC1-1 Google Maps safe import

Date: 2026-10-17 JST

## Outcome

`PC1-1.xlsx` was dry-run against Production in an explicit READ ONLY transaction, imported
through `build_write_repositories().provenance.import_maps_results()`, and reconciled READ ONLY.
The import completed successfully. No migration or DDL was executed, no clinic was created,
deleted, or merged, and no UUID changed.

The input SHA-256 was
`bd6454bc50a36f770bdbd331f5ce3c79857d71e6fe1ee33f09c5650cf1dab966`. The Repository batch ID
was `8bc897b37f3006b6b1a79589677a10a02e52751f7363ed7b5b147799abaea495`.

## Root cause and query plan

The legacy per-row official-number lookup caused repeated sequential scans. The live partial
index is:

```sql
CREATE INDEX idx_clinics_active_base_clinic_id
ON public.clinics ((base_json->>'clinic_id'))
WHERE merged_into IS NULL
  AND COALESCE(base_json->>'clinic_id','')<>'';
```

The optimized prefetch now separates internal ID and canonical `clinic_id` lookups, repeats the
partial-index predicate verbatim, and limits each indexed `ANY`/`unnest` query to 5,000 inputs.
The unindexed compatibility field `base_json.medical_institution_number` is scanned once for
nonblank values and filtered in memory. Phone and name/address candidate queries are chunked in
the same way. `_find_target_prefetched()` is unchanged, so priority, phone scoring, the 30-point
ambiguity gap, name/address matching, and NOT_FOUND behavior remain identical.

Measured Production plans for a representative input:

- Canonical `clinic_id`: `Index Scan using idx_clinics_active_base_clinic_id`, 1 row, 6.424 ms.
- Legacy `medical_institution_number`: `Seq Scan`, 0 rows, 13,849.142 ms.

The complete 38,837-row prefetch took 130.225 seconds in dry-run and 131.805 seconds during the
import. The complete import used 391 SQL executions and took 288.584 seconds (134.58 rows/s).

## Dry-run and matching

| Result | Count |
|---|---:|
| Input rows | 38,837 |
| Linked to an internal clinic ID | 38,837 |
| Unique linked clinics | 38,837 |
| Medical institution number match | 38,834 |
| Phone match | 3 |
| Name + address match | 0 |
| Match ambiguity from Clinic Master lookup | 0 |
| Unlinked | 0 |
| Collector `MAPS_AMBIGUOUS` | 3,668 |
| Collector `MAPS_NOT_FOUND` | 720 |
| Collector `MAPS_MATCHED_WEBSITE` | 25,704 |
| Collector `MAPS_MATCHED_NO_WEBSITE` | 8,745 |

Collector `MAPS_AMBIGUOUS` and `MAPS_NOT_FOUND` remain their distinct statuses; neither was
converted to a no-website result.

## Website protection, conflicts, and duplicates

- All 25,704 incoming website rows were linked and reflected in `maps_website_url`.
- 12,909 had a blank prior current Maps URL; 12,791 already had the same current Maps URL.
- Four confirmed Maps URLs changed. Three are same-domain HTTP/HTTPS or `www` canonicalization;
  one is an exact medical-number match moving from a former dedicated domain to the current
  clinic page. These are permitted confirmed-to-confirmed updates under the existing rule.
- 67 existing confirmed Maps URLs were protected from weaker incoming outcomes.
- 1,997 incoming Maps URLs differed from an existing `hp_url`; `hp_url` is a separate field and
  was not overwritten. Post-import comparison confirmed `hp_url` unchanged for all 38,837 rows.
- Existing Maps overlap: 19,128 rows by medical number/internal clinic, 15,321 by profile URL,
  and 15,321 exact profile+website pairs. The exact Repository batch was absent before import.
- No two input rows resolved to the same internal clinic ID.

## Production reconciliation

| Table / invariant | Before | After | Delta |
|---|---:|---:|---:|
| `public.clinics` | 164,199 | 164,199 | 0 |
| `provenance.google_maps_results` | 67,975 | 106,812 | +38,837 |
| `provenance.import_batches` | 18 | 19 | +1 |
| `provenance.change_history` | 284,455 | 321,220 | +36,765 |
| UUID changes | — | — | 0 |

The stored batch contains 38,837 rows, all linked; 25,704 carry website URLs, 3,668 retain
`MAPS_AMBIGUOUS`, and 720 retain `MAPS_NOT_FOUND`. Full current-state reconciliation found zero
Maps status/URL mismatches, verified all 67 protected URLs, and found zero `hp_url` changes.
Calling the formal import method again returned `already_imported=true` and performed no DML.
The imported state was readable through the same Supabase Read Repository used by the UI.

Immediately before import there were no RUNNING HP/research jobs, no other active
`clinic_runtime` transaction, and no active Email auto-run. Email used its dedicated
`clinic_email_runtime` role. No HP/Email job was started, stopped, reset, or otherwise changed.

## Verification

- Focused Maps/Repository tests: 92 passed.
- Full suite: 1,899 passed, 55 skipped, 0 failed (the skips are existing optional/artifact
  dependent cases).
- Input status/URL consistency: PASS.
- Existing matching-rule parity and rollback/idempotency tests: PASS.
- Production role: `clinic_runtime`, non-superuser, no BYPASSRLS/CREATEDB/CREATEROLE.
- Production migrations/DDL: 0.
- Comdesk UUID mutations: 0.
- Clinic creates/deletes/merges: 0.
- Gmail operations: 0.

**Overall: SUCCESS**
