# Gmail history backfill readiness (aggregate only)

This document contains cohort-level counts only. Clinic names, Clinic IDs, UUIDs,
email addresses, and Gmail identifiers are intentionally excluded from the PR.

## Local read-only audit

The operator's local 328-row source CSV and separately verified linkage overrides
were processed with the dry-run planner under Python 3.12.14. Temporary output
files were removed after the aggregate assertions completed.

- Clinic ID matches: 328 / 328
- UUID values present: 326 / 328
- Latest classification: READY 0 / SKIP 83 / HOLD 245
- Observed logical Gmail events: 116 across 114 clinics
- Backfill candidates: 83 clinics / 85 events
- Duplicate `(Clinic ID, Gmail kind, Gmail ID)` keys: 0
- Historical sheet Draft IDs still unresolved: 74
- Gmail history initially unknown: 215; one clinic was subsequently confirmed by
  matching clinic name and exact master phone, leaving 214 on HOLD
- Same-name alternate Clinic ID review: 88 candidate clinics; one resolved as a
  different clinic using address and phone evidence, 87 remain on HOLD

SKIP means prior Gmail activity is known and automatic recreation is suppressed;
it does not mean the events have been inserted into the database. No production
write, migration, Gmail draft creation, or email sending was performed.

## Reproduce locally

Pass the operator-controlled source CSV and verified override CSV to
`scripts/outreach_gmail_history_backfill.py` with `--dry-run`. The CLI has no
online or apply mode. Do not place those source files in the repository or CI
artifacts.

## Production prerequisites

- Resolve remaining Gmail identity and missing historical Draft ID cases.
- Review all remaining same-name alternate Clinic ID candidates.
- Apply the schema changes only after approval and verify row-level policy and
  privilege behavior in staging.
- Run PostgreSQL-backed idempotency and concurrent reservation tests. Current
  concurrency tests use a fake repository and do not prove database behavior.
- Import approved events with readback checks before considering the cohort
  protected by durable database history.
