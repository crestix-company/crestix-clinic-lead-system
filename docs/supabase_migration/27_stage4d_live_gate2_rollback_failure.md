# Stage4-D Live Gate 2 — Rollback Canary + Failure Injection

Date: 2026-10-07. Project `hwdvyhunozporfuefteb` ("clinic-lead-shadow"). Git HEAD: `b31f052`
(unchanged by this Gate — no code changes, pure DB-side testing). Scope: verify `clinic_runtime`
WRITE permissions genuinely work, transaction rollback is complete, failures never leave
partial writes, and no silent SQLite fallback occurs. Persistent Canary, WRITE Cutover, and
Stage5 are explicitly not started by this Gate.

## 1. Sequence rule honored

Per the Owner's explicit instruction, `public.clinics.id` and `provenance.source_records.id`
default-identity sequences were never consumed this Gate — every INSERT into those two tables'
*identity* path was avoided entirely; the one `clinics` mutation test used `UPDATE` (which never
touches a sequence) against an existing row, not an identity-generating `INSERT`. Verified
before and after this entire Gate: both sequences' `last_value = NULL` (never consumed).
Sequence generation itself is explicitly deferred to the Persistent Canary, not tested here.

## 2. Preflight

- Git HEAD: `b31f052`, clean.
- `public.clinics` = 162,258; 19-table total = 632,903.
- Production SQLite SHA-256: unchanged, all 3 files.
- Security Advisor: 4 INFO (out-of-scope tables only), 0 ERROR, 0 WARNING.
- `clinic_runtime`: `rolsuper=false, rolbypassrls=false, rolcreatedb=false, rolcreaterole=false`.
- anon/authenticated/PUBLIC unintended grants on the 19 tables: 0.
- `CLINIC_WRITE_BACKEND` default: `sqlite` (unchanged).
- Supabase business row writes since Gate 1: 0.

## 3. Connection separation — genuine `clinic_runtime` context, not admin

`postgres` had `admin_option=true` on `clinic_runtime` (Supabase's own default — lets the admin
manage the role) but **not** `inherit_option` or `set_option` — so `SET ROLE clinic_runtime`
was denied at first ("permission denied to set role"). This was itself a useful confirmation
that the admin connection does *not* silently inherit `clinic_runtime`'s privileges. Granted a
temporary, narrowly-scoped `SET` capability (`GRANT clinic_runtime TO postgres WITH INHERIT
false, SET true`) for the duration of this Gate only, so every canary test below genuinely runs
as `current_user = clinic_runtime` (confirmed per-test), with `effective rolbypassrls = false`
(confirmed via `pg_roles` lookup on `current_user` inside the test transaction, not assumed).
**Every single canary statement in this Gate used `BEGIN; SET LOCAL ROLE clinic_runtime; ...`**
— none were run as `postgres`/admin and reported as a `clinic_runtime` result.

At the end of this Gate, the temporary `SET` grant was fully reverted — not just functionally
(`set_option` back to `false`) but structurally: the grant/revoke sequence briefly left a
second, fully inert (`admin=false, inherit=false, set=false`) duplicate membership row; this was
identified and removed (`REVOKE clinic_runtime FROM postgres GRANTED BY postgres`, targeting
only the self-granted row, never touching the original `grantor=supabase_admin` row). Final
state re-verified: exactly one membership row, `admin_option=true, inherit=false, set=false`,
`grantor=supabase_admin` — byte-identical to before this Gate.

## 4. Canary design

Canary FK anchor: `public.clinics.id = 1` (an existing real clinic, confirmed before testing to
have **no** existing `research_results`, `manual_overrides`, or `hp_pages` rows — so every
UPSERT test below is unambiguously "insert new," not "silently overwrite existing data," and
restoration is trivially verifiable). This row is only ever referenced (FK target) or
transactionally `UPDATE`d-then-rolled-back — never left mutated. All other canary identifiers
use the `__stage4d_rb_canary*`/`__canary*` prefix, distinguishable from any real business data,
and no new canary table was created (per instruction — only existing schema/contract used).

## 5–9. Rollback canaries — all PASS, zero persistent leakage

| Test | Tables | Mid-transaction result | Post-rollback leak |
|---|---|---|---|
| §5 INSERT rollback | `app_config.settings` | row visible, `current_user=clinic_runtime` | 0 |
| §6 UPDATE rollback | `public.clinics` id=1 | `active` false→true, `hp_status` UNRESEARCHED→REVIEW | row restored exactly: `active=false, hp_status=UNRESEARCHED`, `medical_key`/`uuid` unchanged, `base_json` byte-identical to pre-Gate read |
| §7 UPSERT rollback | `research_results`, `hp_pages`, `provenance.manual_overrides`, `research.search_cache` | all 4 inserted; `research_results` **UPSERTed a second time with a different payload inside the same transaction** — exactly 1 row remained (not 2), payload reflected the second write, proving `ON CONFLICT DO UPDATE` idempotent-replace, not duplication | 0 for all 4 |
| §8 Job rollback | `research.research_jobs`, `research.research_job_items` | job created, item created → `RUNNING` → `DONE`, job → `COMPLETED` | 0 job rows, 0 item rows |
| §9 Audit atomicity | `provenance.change_history` + `research_results` in the same transaction as §7 | both committed together mid-transaction | both gone together after rollback (0/0) |

## 10. medical_key guard — live verification basis

The `medical_key` transition rule itself is pure Python (`src/master/identity_contract.py`),
intentionally backend-independent — it runs identically regardless of which database executes
the resulting SQL, and is already exhaustively covered by `tests/test_stage4d_write_repository.py`
(all 6 required transitions + edges, re-run in §13 below, still PASS). What *is* specifically a
live-Supabase-under-`clinic_runtime` concern — "does the role have exactly the privilege the
guard's allowed output requires, no more, no less" — is proven directly by §6 (a projection-style
`UPDATE` on `public.clinics` succeeds under `clinic_runtime`'s actual grant+RLS) and §18 (DDL/
schema-level operations are denied). No business row's `medical_key` was read-modified-written
by this Gate; clinic id=1's `medical_key` was confirmed unchanged (`""`, same as the pre-Gate
read) throughout.

## 11–15. Failure injection — all PASS, zero partial writes

| § | Injection | Error raised | Leak after |
|---|---|---|---|
| 11 | Unique violation: duplicate `app_config.settings` key inserted twice (not upserted) in one transaction | `23505 duplicate key value violates unique constraint "settings_pkey"` | 0 |
| 12 | FK violation: `research.research_job_items.clinic_id = 999999999999` (nonexistent), in a transaction that *also* successfully created a fresh canary job first | `23503 ... violates foreign key constraint "research_job_items_clinic_id_fkey"` | 0 for **both** the job item attempt and the earlier, otherwise-successful job creation in the same transaction — confirming the whole transaction aborted atomically, not just the failing statement |
| 13 | Invalid input: `treatment.clinic_research_status.research_status = 'NOT_A_VALID_STATUS'` | `23514 ... violates check constraint "clinic_research_status_research_status_check"` | 0 |
| 14 | Forced exception mid-transaction: successful `research_results` UPSERT + successful `change_history` INSERT, then a forced `1/0` before COMMIT | `22012 division by zero` | 0 for both the primary mutation and the audit row — `research_results` for clinic 1 confirmed back to `NULL` (not even the pre-test state, since clinic 1 never had a row there — full rollback, not partial) |
| 15 | Statement timeout: `SET LOCAL statement_timeout='50ms'; INSERT ...; SELECT pg_sleep(1);` | `57014 canceling statement due to statement timeout` | 0 (the INSERT before the timeout never committed); a fully independent follow-up query on the same project succeeded normally, confirming session/connection recovery |

No test in §11–15 required a destructive statement against real schema objects; none were run.

## 16. Unknown commit outcome — idempotency, not re-tested as a live network-drop

Per the Owner's explicit instruction, a real live network disconnect after COMMIT was **not**
attempted (deliberately avoided — it would create genuine, unnecessary persistent uncertainty
on the live project). Idempotency under "commit succeeded, client never got the ack" is instead
re-confirmed via the existing, already-passing mocked/rollback-safe test suite
(`tests/test_stage4d_write_repository.py`): Treatment (`test_..._retry_after_external_success_no_duplicate`),
HP (`test_..._idempotent_same_item_twice_tracks_attempts`), jobs
(`test_crash_recovery_returns_running_item_to_pending`, `test_job_budget_pause_resume_done_not_researched`),
search (`ON CONFLICT(query_key)`), settings (`ON CONFLICT(key)`) — all re-run in §13, 0 failures.
The live §7 double-UPSERT-in-one-transaction result (exactly 1 row, not 2) is the one live,
non-mocked data point directly supporting this: re-submitting the same natural key never
duplicates a business row.

## 17. No silent fallback

`src/repository/supabase_write_adapter.py` was re-inspected: every write method's exception
handling path is `except Exception as exc: self._conn.rollback(); raise exc` — there is no
branch anywhere in that file, or in any Supabase adapter file, that calls into
`src.repository.sqlite_write_adapter` or `ClinicStore` on failure. This was true before this
Gate and is unchanged by it (no code was touched this Gate). Empirically: every failure
injection in §11–15 raised its exception back to the caller; `CLINIC_WRITE_BACKEND` stayed
`sqlite` (its untouched default) throughout; Production SQLite SHA-256 is unchanged (re-verified
after this entire Gate, all 3 files) — i.e., nothing in this Gate's Supabase-side testing wrote
to SQLite, consistent with there being no fallback path for it to take.

## 18. RLS negative test — DDL/schema operations denied

```
has_schema_privilege(current_user, 'public', 'CREATE')  -> false
has_table_privilege(current_user, 'public.clinics', 'TRUNCATE') -> false
CREATE TABLE public.__should_never_exist_canary(x int);  -- as clinic_runtime
  -> ERROR 42501: permission denied for schema public
```

No object was created (the permission check fails before any DDL executes); confirmed via
`information_schema.tables` after the attempt — the table does not exist.

## 19–20. Final reconciliation

```
settings_leak=0, research_results_leak=0, hp_pages_leak=0, overrides_leak=0, cache_leak=0,
audit_leak=0, jobs_leak=0, job_items_leak=0, treatment_status_leak=0, ddl_denial_leak=0
clinics_count=162258, 19-table total=632903
clinics_id_seq.last_value=NULL, source_records_id_seq.last_value=NULL (never consumed)
clinic id=1: active=false, hp_status=UNRESEARCHED, medical_key="", uuid unchanged,
  base_json byte-identical to the pre-Gate read
```

## 21. Security recheck

Fresh Security Advisor (after all canary/failure tests AND after the role-membership cleanup):
**4 INFO (out-of-scope tables only), 0 ERROR, 0 WARNING** — identical to the post-Gate-1
baseline. `clinic_runtime.rolbypassrls = false` (re-confirmed). anon/authenticated/PUBLIC
unintended grants: 0.

## 22. Full application regression

```
pytest:      1087 passed, 25 skipped, 0 failed
parity:      46/46 PASS
comparator:  cases=38, matched=38, mismatch=0
UI baseline: 営業対象=997, Comdesk=997, UUIDあり=515, UUIDなし=482, 眼科=68, keyword=747 — all MATCH
             Treatment=PASS, pagination_page2=PASS, HP_A_leq_AB=PASS
```

All re-run *after* the live canary/failure testing (not just before), confirming the live
testing itself left the project in the same state the offline-verified suite expects.

## ROLLBACK + FAILURE GATE

1. **HEAD**: `b31f052` (no code changes this Gate)
2. **Runtime role used**: `clinic_runtime`, via genuine `SET LOCAL ROLE` per transaction (not `postgres`/admin) — confirmed per-test via `current_user`
3. **INSERT rollback**: PASS
4. **UPDATE rollback**: PASS (exact restoration verified)
5. **UPSERT rollback**: PASS (4/4 tables; double-UPSERT idempotency proven live)
6. **Jobs rollback**: PASS
7. **Audit atomicity**: PASS
8. **Unique violation**: PASS (23505, 0 leak)
9. **FK violation**: PASS (23503, 0 leak — including the earlier successful statement in the same transaction)
10. **Invalid input**: PASS (23514, 0 leak)
11. **Forced exception**: PASS (22012, 0 leak for both primary + audit)
12. **Timeout**: PASS (57014, 0 leak, connection recovered)
13. **Unknown commit outcome**: PASS (via existing idempotency test suite + live double-UPSERT evidence; no live network-drop attempted, per instruction)
14. **Silent SQLite fallback count**: 0
15. **Partial writes**: 0 (across all 10 canary/failure scenarios)
16. **Persistent unexpected writes**: 0
17. **Sequence `clinics_id_seq` state**: unconsumed (`last_value=NULL`), unchanged from Gate 1
18. **Sequence `source_records_id_seq` state**: unconsumed (`last_value=NULL`), unchanged from Gate 1
19. **`public.clinics` count**: 162,258 (unchanged)
20. **19-table count**: 632,903 (unchanged)
21. **UUID integrity**: PASS
22. **medical_key integrity**: PASS
23. **Production SHA**: unchanged, all 3 files
24. **Security Advisor**: 0 ERROR, 0 WARNING (4 INFO, out-of-scope tables only)
25. **pytest**: 1087 passed, 0 failed, 25 skipped
26. **Parity**: 46/46 PASS
27. **Comparator**: 38 cases, 0 mismatch
28. **UI baseline**: exact (all 6 + Treatment + HP + pagination)
29. **Remaining blockers**: none for this Gate. One self-inflicted, fully-cleaned-up artifact occurred and is documented transparently (§3's temporary role-membership grant/revoke, including a brief redundant inert row) — not hidden, fully reverted and re-verified byte-identical to the pre-Gate state.
30. **ROLLBACK + FAILURE GATE = PASS**

Next step this Gate authorizes moving toward: **Persistent Canary + Reconciliation** — not
started by this pass. WRITE Cutover not started.
