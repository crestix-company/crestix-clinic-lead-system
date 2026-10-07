# Stage4-D Live Gate 4 — Production WRITE Cutover

Date: 2026-10-07. Starting HEAD:
`daae4a85172ae38cd77e9291567b782fef3698cf`.

## Preflight

The pre-cutover state matched the approved checkpoint exactly:

- `public.clinics=162258`; 19-table total `632903`.
- UUID duplicate groups 0; non-blank medical_key duplicate groups 0.
- Both high-range sequences remained at `1000000000`; no reset or nextval occurred.
- `clinic_runtime`: LOGIN, NOSUPERUSER, NOBYPASSRLS, NOCREATEDB, NOCREATEROLE.
- Unintended anon/authenticated/PUBLIC WRITE grants on the 19 tables: 0.
- Security Advisor: ERROR 0 / WARNING 0.
- pytest 1096 passed / 0 failed / 25 skipped; critical skips 0.
- parity 46/46; comparator 38/38, mismatch 0.
- UI baseline exact: 997 / 997 / 515 / 482 / 68 / 747; Treatment, HP, pagination PASS.
- All three Production SQLite SHA-256 values matched the approved baseline.

## Production configuration cutover

The single tracked routing source is now `config/production_runtime.env`:

```text
CLINIC_DATA_BACKEND=supabase
CLINIC_WRITE_BACKEND=supabase
```

The cross-platform Python launcher reads this tracked routing file and then the ignored,
mode-600 `.supabase-runtime.env.local`, which contains `SUPABASE_RUNTIME_DB_URL`. Mac and both
PowerShell launchers use that Python entry point. No secret is present in Git, documentation,
logs, or console output. Runtime READ/WRITE, Treatment, and HP factories never fall back to
`SUPABASE_DB_URL`; that variable remains admin/migration-only.

An explicit process value `CLINIC_WRITE_BACKEND=sqlite` overrides the production file and is
the Stage5-era emergency rollback switch. It changes only future write destination. It does not
copy, replay, synchronize, or dual-write any successful mutation.

## Actual production-routed WRITE

`scripts/supabase_migration/stage4d_write_cutover_verify.py` loaded the actual production
configuration, removed the admin URL from its process, constructed the normal WRITE Repository,
and authenticated directly as `clinic_runtime`.

The canary wrote reserved numeric marker `1000000007` to the existing `monthly_limit` setting,
read the exact value back as `clinic_runtime`, and restored the original canonical JSON value
through the same Repository. The first candidate key (`filter_defaults`) was absent; its
precondition stopped before any write, and the script was corrected to require the existing
`monthly_limit` row.

Expected Supabase mutations: one settings UPDATE plus one exact restoring UPDATE. Net row/count
delta: 0. Unexpected writes: 0. Audit/history rows: none required by the settings contract.
SQLite fallback and dual-write: 0. The three SQLite SHA values were identical before and after.

## Routing and startup proof

A production-config startup simulation ran with `SUPABASE_DB_URL` removed. READ, WRITE,
Treatment, and HP repositories all connected through `SUPABASE_RUNTIME_DB_URL`; identity was
`current_user=session_user=clinic_runtime`. `refresh_age_model` completed as a no-op because
the stored signature was current, metrics loaded successfully, and background repository
initialization completed without unsupported calls.

Startup result: unsupported calls 0, admin dependency 0, row writes 0, counts unchanged.
Static runtime audit found:

- A+B direct SQLite business WRITE: 0.
- Hardcoded SQLite adapter construction in `app_v2.py`, `jobs.py`, `search_provider.py`,
  `google_maps.py`, `research_worker.py`, and `hp_research_batch.py`: 0.
- SQLite compatibility adapters exist only behind Repository backend factories.
- Normal Supabase methods raising `BackendNotSupportedError` or `NotImplementedError`: 0.
- Supabase failure-to-SQLite/Treatment-sidecar/HP-sidecar fallback paths: 0.

Treatment and HP backend selection was moved from the workers into dedicated Repository
factories, so business workers cannot select sidecar adapters directly.

## Workflow verification

The 174-test focused runtime suite covered Repository routing plus job create/claim/pause/resume/
finish/budget/recovery, CachedSearch atomic budget reservation/cache/usage, Google Maps matching,
website preservation/audit/projection, Treatment idempotency, and HP idempotency. The earlier
persistent canary remains the live proof for those business write contracts; they were not
repeated against production business rows during cutover.

Post-cutover full pytest: **1097 passed, 0 failed, 25 skipped**, critical skips 0.
Parity: **46/46 PASS**. Comparator: **38/38**, mismatch 0. UI baseline remained exact.

## Final reconciliation and security

- `public.clinics=162258`; 19-table total `632903`.
- UUID duplicate groups 0; medical_key duplicate groups 0.
- Known medical_key mutations 0; existing PK rewrites 0.
- `clinics_id_seq.last_value=1000000000` and
  `source_records_id_seq.last_value=1000000000`; no reset/consumption.
- Fresh Security Advisor: ERROR 0 / WARNING 0 (four unchanged out-of-scope INFO findings).
- Unintended anon/authenticated/PUBLIC WRITE grants: 0.
- Unexpected Supabase mutations 0; partial writes 0; SQLite fallback 0; dual-write 0.
- Production SHA unchanged:
  - Clinics `fc53c9de851567c62f75a18aceb9521cb94b82385e69c19f09e880df386cc8a0`
  - Treatment `36dbe570d19af61ca45c8571338a2cb3e3c28e472b42113286e0a0e093add804`
  - HP `3980865ebd0de6f30dcebad98b1aada92252b00b3e7836874b408ffa1dbf7d53`

## Final architecture

- READ Primary: Supabase.
- WRITE Primary: Supabase.
- Runtime identity: `clinic_runtime` via `SUPABASE_RUNTIME_DB_URL`.
- SQLite, Treatment sidecar, and HP sidecar: emergency rollback/archive compatibility only.
- Stage5 will remove the remaining compatibility/runtime dependencies; it was not started here.

**STAGE4-D WRITE CUTOVER = PASS**

**STAGE4-D = PASS**

**STAGE5 READY = YES**
