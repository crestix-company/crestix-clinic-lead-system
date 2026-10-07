# Stage4-D Gate 3.5 — Production WRITE Runtime Closure

Date: 2026-10-07. Starting HEAD: `9ecfd010282ebccac38d9103d27cb96762598136`.
READ Primary remained Supabase and WRITE Primary remained SQLite for the entire gate. No WRITE
cutover, dual-write, Stage5 work, sequence reset, or production business-row mutation occurred.

## Blockers found and closed

The production audit found seven blockers: the existing database URL authenticated as the admin
role; four clinic and six jobs Repository methods were unsupported; `jobs.py` and
`search_provider.py` instantiated SQLite adapters directly; startup called the unsupported age
refresh; and the Mac/Windows launch path had no secure runtime credential contract.

All blockers are closed:

- Runtime application connections require `SUPABASE_RUNTIME_DB_URL`. Neither READ nor WRITE,
  Treatment, nor HP runtime adapters fall back to `SUPABASE_DB_URL`.
- `SUPABASE_DB_URL` remains the admin/migration-only contract.
- `SupabaseClinicWriteRepository` implements `import_comdesk`, `import_master`,
  `resolve_review`, and `refresh_age_model`, including digest/idempotency, canonical matching,
  source/original rows, audit history, projections, UUID rules, and authoritative-only
  `medical_key` completion.
- `SupabaseJobsWriteRepository` implements filter-based creation, crash recovery, completion,
  specific-item claim, budget/pause requeue, finish, status reads, recent jobs, and lane inputs.
- Jobs, automatic research, and cached search now resolve through `write_repositories_for()`.
  The search budget check and reservation remain one atomic transaction.
- Supabase research save/override now update audit and clinic projection in the same transaction,
  matching the canonical SQLite behavior.
- Normal Supabase WRITE methods contain zero `BackendNotSupportedError` or
  `NotImplementedError` paths.

`repair_reset_job_items` remains an explicitly invoked SQLite historical-repair/admin path; it
is not part of ordinary app startup or research execution. SQLite adapter construction remains
inside the formal backend factories only, including the Treatment and HP factory branches.

## Genuine runtime identity and grants

A dedicated password was generated for the existing `clinic_runtime LOGIN` role and stored only
in the local, mode-600, Git-ignored `.supabase-runtime.env.local`. No credential or connection
URL was printed or committed. The Session Pooler runtime connection authenticated directly as:

`current_user=clinic_runtime`, `session_user=clinic_runtime`, with `rolsuper=false`,
`rolbypassrls=false`, `rolcreatedb=false`, and `rolcreaterole=false`.

Catalog review found one missing operation needed by canonical review resolution. The runtime
schema and live role received only `UPDATE` on
`provenance.comdesk_original_rows`. No DELETE, DDL, TRUNCATE, TRIGGER, REFERENCES, role, or
BYPASSRLS capability was added. Unintended anon/authenticated/PUBLIC WRITE grants across the 19
migration tables remain 0.

## Launcher contract

`scripts/launch_v2.py` is the single cross-platform loader for
`.supabase-runtime.env.local`. It accepts only `SUPABASE_RUNTIME_DB_URL`,
`CLINIC_WRITE_BACKEND`, and `CLINIC_DATA_BACKEND`, preserves already supplied process values,
never prints values, requires mode 600 on POSIX, and fails explicitly if Supabase WRITE is
selected without the runtime URL. The Mac shell and both Windows/PowerShell launchers use this
same Python entry point. At the future cutover, one ignored configuration value changes:
`CLINIC_WRITE_BACKEND=supabase`. The code default remains `sqlite` in this gate.

## Startup simulation and safety

A production-shaped process set `CLINIC_DATA_BACKEND=supabase` and
`CLINIC_WRITE_BACKEND=supabase`, removed `SUPABASE_DB_URL`, and constructed READ, WRITE,
Treatment, and HP runtime repositories using only `SUPABASE_RUNTIME_DB_URL`. It then executed
the ordinary startup `refresh_age_model()` path. The stored age-model signature was current, so
the operation was a no-op.

Results: identity `clinic_runtime`; admin env present false; unsupported calls 0; row writes 0;
all 19 table counts unchanged; `public.clinics=162258`; 19-table total `632903`.

## Regression and reconciliation

- Focused WRITE Repository suite: 67/67 PASS.
- Full pytest: 1096 passed, 0 failed, 25 skipped; critical runtime skips 0.
- Parity harness: 46/46 PASS.
- Comparator: 38/38 matched, mismatch 0.
- UI: 営業対象 997; Comdesk 997; UUIDあり 515; UUIDなし 482; 眼科 68;
  keyword「クリニック」747; Treatment PASS; HP PASS; pagination PASS.
- UUID duplicate groups 0; non-blank medical_key duplicate groups 0.
- Sequences unchanged: clinics and source_records both `last_value=1000000000`.
- Fresh Security Advisor: ERROR 0, WARNING 0 (four unchanged out-of-scope INFO findings).
- Unexpected Supabase row writes 0; SQLite fallback 0; partial writes 0.
- Production SHA-256 unchanged:
  - Clinics: `fc53c9de851567c62f75a18aceb9521cb94b82385e69c19f09e880df386cc8a0`
  - Treatment: `36dbe570d19af61ca45c8571338a2cb3e3c28e472b42113286e0a0e093add804`
  - HP: `3980865ebd0de6f30dcebad98b1aada92252b00b3e7836874b408ffa1dbf7d53`

## Gate result

All production WRITE runtime blockers are closed. Normal runtime admin credential dependency,
direct SQLite business-layer WRITE routing, unsupported Supabase WRITE calls, and silent
fallback are all 0. Remaining blocker for this gate: none.

**PRODUCTION WRITE RUNTIME READY = YES**

WRITE Primary is still SQLite. WRITE Cutover and Stage5 have not started.
