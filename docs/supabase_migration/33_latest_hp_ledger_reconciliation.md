# Latest Completed HP Job Ledger Reconciliation

Date: 2026-10-07

## Scope and method

This was a missing-only repair for the latest fully completed HP research job. No web research,
website fetching, job rerun, clinic update, research-result update, SQLite access, or historical/global
backfill was performed. The read-only dry-run and the apply both used the `clinic_runtime` identity.

The reconciliation reused `src.master.jobs._hp_ledger_payload`, the same conversion helper used by
the corrected HP worker. The repository insert path uses `ON CONFLICT(clinic_id) DO NOTHING`; all
preconditions, inserts, and in-transaction checks ran in one SERIALIZABLE transaction. The job ID
was pinned from dry-run output for apply.

## Reconciliation result

- Latest HP job: `b27c7d64aad54132a697d9d44104e81b`
- Job state: COMPLETED; 9 items, all 9 DONE
- Persisted `research.research_results`: 9 of 9
- Canonical ledger before: 0 of 9; dry-run found all 9 reconstructable
- Inserted: 9 missing ledger rows; existing rows overwritten: 0
- Derived canonical statuses: 7 `OK`, 2 `REVIEW` (the two REVIEW outcomes were not coerced to ERROR)
- Successful clinics with UUID: 2; successful clinics with blank UUID: 5
- No raw URLs or credentials are recorded here.

The mapping leaves machine HP rank, score, candidate ranks, ambiguity reason, and feature JSON at
the same blank/default values emitted by the current worker contract; the persisted research result
does not contain the separate HP ABC v2 batch payload. Attempts is 1 for a missing first ledger row,
elapsed seconds is 0 as in the worker contract, and `portal_name` is derived through the existing
`portal_name_for_url()` helper. `researched_at`, result status, HP URL, categories, and error detail
come from the persisted research result / worker mapping.

## Validation

- Clinic count: 162,258 before and after
- UUID duplicate groups: 0
- medical_key duplicate groups: 0
- Active HP jobs / other runtime transactions at apply: 0 / 0
- Latest-job ledger rows after: 9; missing: 0
- Step4 Tokyo × medical, force OFF: 9 before → 0 after
- Step4 force ON: 10,305 eligible overall; all 9 job clinics remain eligible
- Step5 current-job summary: 9 items, 7 success, 2 failed/review, 2 successful UUID-present,
  5 successful UUID-blank, 5 export-eligible
- In-memory generated Comdesk CSV: 5 data rows; count matches Step5; historical leakage: 0
- Unexpected production mutation: 0
- Focused tests: 20 passed
- Full pytest: 1,111 passed, 0 failed, 50 skipped

The first post-commit validator run stopped after the transaction had committed because a literal
`%` in a parameterized PostgreSQL LIKE pattern was interpreted by psycopg as placeholder syntax.
The SQL literal was escaped (`%%` in the client query); a subsequent read-only verification
confirmed the committed 9 rows, Step4 counts, and 5-row current-job CSV. No second write was made.
