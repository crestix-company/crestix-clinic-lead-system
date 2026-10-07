# Stage4-D Persistent Canary + Reconciliation

Date: 2026-10-07. Project `hwdvyhunozporfuefteb` (clinic-lead-shadow). Starting Git HEAD
after the Gate 2 checkpoint: `daf5686`. WRITE Primary remained SQLite throughout. WRITE Cutover
and Stage5 were not started.

## 1. Handoff verification and preflight

The handoff was verified read-only before any new live write. HEAD was `b31f052`, branch was
`feature/supabase-shadow-migration`, and `git diff --check` passed. The untracked Gate 2 report
matched the actual live state and was committed alone as `daf5686` (`docs: record Stage4-D
rollback and failure gate`).

Live preflight matched the report exactly: `public.clinics=162258`, 19-table total `632903`,
both reserved sequences unconsumed (`last_value=NULL`, start `1000000000`), all 19 target tables
had RLS and the `clinic_runtime_rw` policy, unintended anon/authenticated/PUBLIC grants were 0,
and the runtime role was NOSUPERUSER/NOBYPASSRLS/NOCREATEDB/NOCREATEROLE. The original single
`postgres` membership row was `admin=true, inherit=false, set=false, grantor=supabase_admin`.
Security Advisor was ERROR 0 / WARNING 0 (4 INFO on the same out-of-scope tables).

Production SQLite SHA-256 values matched the handoff:

- clinics: `fc53c9de851567c62f75a18aceb9521cb94b82385e69c19f09e880df386cc8a0`
- Treatment: `36dbe570d19af61ca45c8571338a2cb3e3c28e472b42113286e0a0e093add804`
- HP: `3980865ebd0de6f30dcebad98b1aada92252b00b3e7836874b408ffa1dbf7d53`

## 2. Canary design and runtime role

Token: `__stage4d_persistent_canary_20261007_codex_01`. No existing business clinic was
modified. The business writes used the production Supabase Repository adapters with
`current_user=clinic_runtime`; the role remained non-superuser and non-BYPASSRLS during and
after the writes. As in Gate 2, admin was used only to grant/revoke temporary SET ROLE ability
and for read-only reconciliation. The original role-membership row was restored exactly.

`monthly_limit` was the settings canary: its original JSON text `1000` was snapshotted, a token
value was observed after the Repository write, and `1000` was restored immediately through the
same Repository. Final settings row count and value are unchanged.

## 3. First attempt: defect found, no concealment

The clinic insert committed and generated `public.clinics.id=1000000000`; its required
`医院追加` history row committed atomically. The immediately following Repository
`refresh_projection` failed because the shared SQLite-compatible projection helper returns
`active`/`owner_equal` as 0/1, while the Supabase adapter passed that smallint directly to
PostgreSQL boolean columns. PostgreSQL correctly rejected it with `DatatypeMismatch`.

At that point the exact partial state was one isolated clinic plus one audit row. Settings had
not been touched, `source_records_id_seq` remained unconsumed, the temporary role grant had been
reverted, and all three SQLite SHA values were unchanged. This incident is not represented as a
clean first-pass success.

The minimal uncommitted fix converts only `active` and non-NULL `owner_equal` to Python bool at
the Supabase bind boundary, preserving the pure helper's SQLite parity behavior. A regression
test was added; the focused Repository suite passed 59/59. The runner then resumed only after
verifying that the existing token row was the exact insert-only state and no downstream source
record existed.

## 4. Persistent writes and reconciliation

The resumed canary completed under `clinic_runtime`:

| Path | Expected | Actual |
|---|---|---|
| clinic | generated high-range PK, token UUID, blank medical_key | id `1000000000`, UUID exact, medical_key `""` |
| source record | generated high-range PK and FK to canary clinic | id `1000000000`, clinic_id exact |
| research result | token JSON/text | 1 exact row |
| HP page | token URL and JSON | 1 exact row |
| job lifecycle | PENDING → RUNNING → DONE; job COMPLETED | exact, 1 job + 1 item |
| settings | token observed, then original value restored | PASS, final `monthly_limit=1000` |
| Treatment | DONE status + 1 CONFIRMED category | exact, FK/boolean/text/timestamp verified |
| HP | OK/DONE upsert, JSON, attempts | exact, attempts=1 |
| history | clinic addition + explicit evidence + automatic research | 3 retained rows |

Both generated IDs meet the reserved floor and exceed the imported maxima (`clinics=162258`,
`source_records=162263`). Final `clinics_id_seq.last_value=1000000000` and
`source_records_id_seq.last_value=1000000000`; neither sequence was reset or manually set.
UUID duplicate groups and non-blank medical_key duplicate groups are both 0. The canary
medical_key remained blank, so no prohibited identity transition occurred.

## 5. Cleanup and retained residue

The settings mutation was fully restored through its Repository. The persistent clinic and its
children were deliberately not deleted: `clinic_runtime` has no DELETE grant on `clinics`,
`source_records`, or append-only `change_history`, and using admin direct delete or temporarily
expanding those privileges would violate the canary/cleanup boundary and erase the audit trail.

Retained rows are exactly: clinic 1; source_records 1; change_history 3; research_jobs 1;
research_job_items 1; research_results 1; hp_pages 1; Treatment status 1; Treatment category 1;
HP result 1. There are no other token rows. Final counts are `public.clinics=162259` and
19-table total `632915`.

Unexpected mutation outside this declared set: 0. SQLite fallback: 0. Final partial/unreconciled
rows inside the declared set: 0. The first attempt did leave a committed prefix (clinic + audit),
which was explicitly detected and then completed using the exact same isolated canary; it is
recorded above rather than hidden.

## 6. Security and regression

- RLS: 19/19; unintended anon/authenticated/PUBLIC grants: 0.
- `clinic_runtime`: NOSUPERUSER, NOBYPASSRLS, NOCREATEDB, NOCREATEROLE.
- Security Advisor: ERROR 0, WARNING 0, four unchanged out-of-scope INFO findings.
- Production SQLite SHA: all three unchanged before/after.
- UI baseline: exact — 営業対象 997, Comdesk 997, UUIDあり 515, UUIDなし 482, 眼科 68,
  keyword「クリニック」747; Treatment/HP/pagination PASS.
- Full pytest: 1081 passed, 25 skipped, 7 failed. All seven failures are exact +1 comparisons
  caused by the retained canary clinic.
- Parity harness: 40/46 PASS; the six failures are exact +1 count/metrics/funnel differences.
- Comparator: 36/38; `funnel` and `metrics` differ only by the retained canary (+1 total and UUID).

## 7. Remaining blocker and gate decision

The live Repository write paths, generated IDs, FK/JSON/boolean/timestamp handling, Treatment,
HP, jobs, audit, identity rules, RLS, and no-fallback behavior are verified. However, the stated
Gate requires pytest failure 0, parity 46/46, and comparator mismatch 0. Those conditions are not
met while the auditable persistent canary remains visible to all-clinic READ metrics.

The next action requires an explicit owner decision: either (a) authorize a narrowly scoped,
Repository-mediated cleanup contract and corresponding runtime privileges for test-only rows,
including the append-only audit implications, or (b) define a supported canary exclusion rule
for production aggregate READs. No admin direct delete, privilege expansion, sequence reset,
WRITE Cutover, or Stage5 action was taken.

## PERSISTENT CANARY GATE

1. Final HEAD: `daf5686` (Gate 2 doc checkpoint; Persistent Canary changes uncommitted)
2. Handoff verification: PASS
3. Rollback/failure Gate doc checkpoint: PASS, commit `daf5686`
4. Runtime role: PASS (`clinic_runtime`)
5. Settings canary: PASS, restored
6. Jobs canary: PASS
7. Research canary: PASS
8. Treatment canary: PASS
9. HP canary: PASS
10. source_records generated ID: `1000000000`
11. clinics generated ID: `1000000000`
12. clinics sequence state: `last_value=1000000000`
13. source_records sequence state: `last_value=1000000000`
14. Generated ID floor: PASS
15. Audit/history: PASS, 3 retained rows
16. Cleanup: settings restored; persistent auditable rows retained
17. Remaining canary rows: 12 total rows across 10 tables (including 3 history rows)
18. Unexpected writes: 0
19. Partial writes: final 0; one detected/resumed committed prefix on first attempt
20. SQLite fallback count: 0
21. Clinics count: `162259`
22. 19-table total: `632915`
23. UUID integrity: PASS (duplicate groups 0)
24. medical_key integrity: PASS (duplicate groups 0; canary blank)
25. Production SHA: unchanged, all 3 files
26. Security Advisor: ERROR 0 / WARNING 0
27. pytest: 1081 passed / 7 failed / 25 skipped
28. parity: 40/46
29. comparator: 36/38, mismatch 2
30. UI baseline: exact, all ancillary checks PASS
31. Remaining blocker: auditable canary cleanup/exclusion contract
32. **PERSISTENT CANARY GATE = FAIL**

`WRITE CUTOVER READY = NO`. WRITE Primary remains SQLite.

---

## 8. Cleanup & Reconciliation Closure

The FAIL above is preserved as the first-pass result. On the same date, the owner explicitly
authorized an exact admin/migration cleanup of these synthetic test rows. No query exclusion,
runtime DELETE grant, RLS weakening, trigger disabling, cascade, or sequence reset was used.

### 8.1 Exact cleanup manifest

The live database was re-inventoried before cleanup. It matched the documented 12 rows exactly;
the cleanup script also locked and verified every complete row using a canonical SHA-256 before
issuing any DELETE:

| Schema/table | Exact PK / natural key | Rows | Dependency / classification |
|---|---|---:|---|
| `public.clinics` | `id=1000000000`, token UUID | 1 | parent; synthetic business state |
| `provenance.source_records` | `id=1000000000`, `clinic_id=1000000000`, token `source_hash` | 1 | child of clinic; synthetic provenance |
| `provenance.change_history` | `id IN (179108,179109,179110)`, `clinic_id=1000000000` | 3 | child of clinic; synthetic audit evidence |
| `research.research_jobs` | `id=0c0d496248c54c99ade63145ab114626` | 1 | parent of job item; synthetic job state |
| `research.research_job_items` | exact job ID + `clinic_id=1000000000` | 1 | child of job and clinic |
| `research.research_results` | `clinic_id=1000000000` | 1 | child of clinic |
| `research.hp_pages` | `clinic_id=1000000000` + exact token URL | 1 | child of clinic |
| `treatment.clinic_research_status` | `clinic_id=1000000000` | 1 | child of clinic |
| `treatment.clinic_treatment_research` | `clinic_id=1000000000` + token category | 1 | child of clinic |
| `hp_research.clinic_hp_research` | `clinic_id=1000000000` | 1 | child of clinic |

All other tables capable of referencing the clinic, source record, or job were independently
verified to have zero matching dependencies. Pre-cleanup counts were `public.clinics=162259`
and 19-table total `632915`. UUID and non-blank medical_key duplicate groups were both 0.
Both sequence `last_value`s were `1000000000`. Security Advisor remained ERROR 0 / WARNING 0,
WRITE Primary was SQLite, and all three production SHA values were unchanged.

### 8.2 Cleanup method and result

`scripts/supabase_migration/stage4d_persistent_canary_cleanup.py` is a Git-managed, one-time
admin/migration cleanup script. It contains only exact identifiers and hashes, checks the
baseline counts/sequences, rejects any unmanifested FK dependency, and deletes children before
parents inside one SERIALIZABLE transaction. It requires every per-table affected-row count and
the final counts/sequences to match before commit.

The transaction removed exactly 12 rows. All three synthetic audit rows were safe to remove
because their exact PKs, clinic FK, timestamps, and full content proved they belonged only to
this canary. This does not add any runtime capability for deleting real audit history.

Post-cleanup independent reconciliation:

- `public.clinics=162258`; 19-table total `632903`.
- Exact synthetic residue: 0 across all 10 manifest tables; intentionally retained rows: 0.
- `clinics_id_seq.last_value=1000000000`, increment 1; inferred next generated ID
  `>=1000000001` without consuming it.
- `source_records_id_seq.last_value=1000000000`, increment 1; inferred next generated ID
  `>=1000000001` without consuming it.
- UUID duplicate groups 0; medical_key duplicate groups 0.
- Unexpected business mutations 0; final partial writes 0; SQLite fallback 0.
- WRITE Primary remains SQLite.
- Production SHA-256 values remain exactly the three values recorded in §1.

### 8.3 Boolean defect closure

The original canary failure remains documented in §3. The fix converts only the Supabase bind
values for `active` and non-NULL `owner_equal` to real Python booleans. The shared projection
helper and SQLite path are unchanged; medical_key logic and all unrelated adapter semantics are
unchanged. Focused Repository tests pass 59/59, including the new PostgreSQL boolean-bind test.

### 8.4 Final regression and security

- Full pytest: **1088 passed, 0 failed, 25 skipped**. The skip count is unchanged from the
  earlier verified suite; critical runtime skips: **0**. No test was xfailed or skipped to hide
  the prior seven count failures.
- Parity: **46/46 PASS**.
- Comparator: **38/38 matched, mismatch 0**.
- UI baseline: 営業対象 997; Comdesk 997; UUIDあり 515; UUIDなし 482; 眼科 68;
  keyword「クリニック」747; Treatment PASS; HP PASS; pagination PASS.
- Fresh Security Advisor: **ERROR 0, WARNING 0** (the same four out-of-scope INFO findings).
- `clinic_runtime`: `rolsuper=false`, `rolbypassrls=false`, `rolcreatedb=false`,
  `rolcreaterole=false`; unintended anon/authenticated/PUBLIC WRITE grants: 0.

## PERSISTENT CANARY RECONCILIATION FINAL

1. Boolean adapter defect: PostgreSQL boolean columns received smallint-compatible projection values.
2. Boolean fix: PASS; explicit bool conversion at the Supabase bind boundary.
3. Canary rows before cleanup: 12 rows / 10 tables.
4. Rows removed: 12 exact rows in one guarded transaction.
5. Rows intentionally retained: 0.
6. Canary business rows remaining: 0.
7. Clinics count: `162258`.
8. 19-table total: `632903`.
9. Clinics sequence: `last_value=1000000000`.
10. Source records sequence: `last_value=1000000000`.
11. Next-ID contract: `>=1000000001` for both sequences, inferred without consumption.
12. UUID integrity: PASS.
13. medical_key integrity: PASS.
14. Unexpected mutations: 0.
15. Partial writes: 0 after final reconciliation.
16. SQLite fallback: 0.
17. Production SQLite SHA: unchanged for all three databases.
18. pytest: 1088 passed / 0 failed / 25 skipped; critical skips 0.
19. Parity: 46/46 PASS.
20. Comparator: 38/38, mismatch 0.
21. UI baseline: exact; Treatment/HP/pagination PASS.
22. Security Advisor: ERROR 0 / WARNING 0.
23. Runtime role: least privilege unchanged; no cleanup DELETE grants added.
24. Remaining blockers: none for this Gate.
25. **PERSISTENT CANARY GATE = PASS**.

`WRITE CUTOVER READY = YES`. WRITE Primary remains SQLite. WRITE Cutover and Stage5 have not
been started.
