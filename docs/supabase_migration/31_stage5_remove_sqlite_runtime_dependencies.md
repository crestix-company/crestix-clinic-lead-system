# Stage5 — Remove SQLite Runtime Dependencies

Date: 2026-10-07 (Asia/Tokyo)

## Purpose and result

Stage5 removes the Stage4-C compatibility wrapper and all persistent SQLite/local-sidecar
dependencies from the production runtime. READ and WRITE remain Supabase through the genuine
`clinic_runtime` credential. Historical SQLite files remain unchanged for explicit
admin/migration/archive/test use.

Code/runtime closure is complete. Final Stage5 acceptance is **not yet complete** because this
Mac cannot provide the required Windows PowerShell 5.1 real-device test or a genuine second-PC
round trip. Those two external acceptance checks are not replaced by same-Mac simulation.

## Removed runtime dependencies

- `app_v2.store_for()` now constructs `SupabaseRuntimeStore` before any `ClinicStore` exists.
- `Stage4CClinicStore` is no longer in the production path: no SQLite fallback, async comparator,
  local log, or delegated SQLite method remains in the Supabase runtime.
- Direct UI SQL was replaced with Supabase facade operations for filter options, Maps/HP job
  candidates, metrics, provenance/history, exports and revision signatures.
- Runtime ComDesk export reads `provenance.templates` and
  `provenance.comdesk_original_rows` directly from Supabase.
- Runtime SQLite backup/reintegration controls were removed from the Supabase UI. They remain
  available only in explicit SQLite admin/test mode.
- Treatment worker opens its historical final SQLite only when
  `CLINIC_WRITE_BACKEND=sqlite` is explicitly selected. Its local progress/cache SQLite files
  remain allowed non-authoritative resumability aids; they are not business SoT.
- HP worker selects candidates and reads attempts from Supabase in production and opens neither
  the clinic DB nor HP/Treatment sidecars.
- Cross-PC job exclusion relies on PostgreSQL row claims/leases; the machine-local file lock is
  used only by explicit SQLite mode.

## Source of truth

| Domain | Production source of truth |
|---|---|
| Clinic master | `public.clinics` |
| Provenance / ComDesk / Maps / history | `provenance.*` |
| Treatment | `treatment.clinic_research_status`, `treatment.clinic_treatment_research` |
| HP | `hp_research.clinic_hp_research`, `research.hp_pages`, `research.research_results` |
| Jobs | `research.research_jobs`, `research.research_job_items` |
| Search | `research.search_cache`, `research.search_usage` |

## Repository architecture

Production startup loads `config/production_runtime.env`, then the ignored mode-600
`.supabase-runtime.env.local`. `SupabaseRuntimeStore` owns the READ repository bundle;
`write_repositories_for()` selects the existing Supabase WRITE adapters. Neither path falls
back to `SUPABASE_DB_URL` or SQLite. Explicit SQLite mode remains for admin/migration/test only.

## DB-path tests and runtime SQLite instrumentation

- All three DB path variables unset: live runtime facade PASS.
- All three variables set to `/definitely/not/exist/...`: Streamlit startup and sales UI PASS.
- `sqlite3.connect` replaced with an exception during live startup/UI/export: no exception was
  raised; persistent SQLite open count = **0**.
- Supabase-only ComDesk export: **997 rows**, SQLite open = 0.
- HP production worker initialization with invalid clinic/Treatment/HP paths and limit 0:
  PASS, SQLite open = 0, DML = 0.
- Local Treatment progress/cache opens are non-authoritative and explicitly permitted by the
  requirements; persistent Treatment sidecar open in production = 0.

## Launchers

- Mac launcher no longer resolves, prints, validates or opens `CLINIC_DB_PATH`; actual launch
  reached the Streamlit server using the secure runtime env.
- Both Windows launchers contain no clinic/Treatment/HP SQLite path contract and require the
  ignored `.supabase-runtime.env.local`. They preserve Windows PowerShell 5.1-compatible syntax,
  UTF-8 BOM and CRLF.
- Windows real-device acceptance remains pending (see blockers).

## Multi-PC and concurrency

PostgreSQL claim/lease logic remains the shared concurrency authority. The Supabase runtime does
not acquire the historical machine-local research lock. Existing job repository tests cover
claim, pause/resume, retry, recovery and idempotent finish without duplicate effects.

A genuine PC-A → Supabase → PC-B acceptance cannot be performed from this single Mac and remains
pending. Same-host processes were deliberately not reported as two PCs.

## Regression and integrity

- Full pytest: **1101 passed, 0 failed, 25 skipped**; critical runtime skips = 0.
- Parity: **46/46 PASS**.
- Comparator: **38/38**, mismatch 0.
- UI baseline: 997 / 997 / 515 / 482 / 68 / 747; Treatment, HP and pagination PASS.
- Mac invalid-path UI: startup PASS, sales = 997, SQLite open = 0.
- Live `public.clinics`: **162258**; 19-table total: **632903**.
- UUID duplicate groups: 0; medical_key duplicate groups: 0; FK orphan rows: 0 across 20 FKs.
- Runtime role: `clinic_runtime`; SUPERUSER/BYPASSRLS/CREATEDB/CREATEROLE all false.
- anon/authenticated/PUBLIC unintended WRITE grants: 0.
- Security Advisor: ERROR 0, WARNING 0 (fresh dashboard observation).
- Unexpected Supabase business writes: 0.

Production SQLite SHA-256 remained unchanged:

- Clinics: `fc53c9de851567c62f75a18aceb9521cb94b82385e69c19f09e880df386cc8a0`
- Treatment: `36dbe570d19af61ca45c8571338a2cb3e3c28e472b42113286e0a0e093add804`
- HP: `3980865ebd0de6f30dcebad98b1aada92252b00b3e7836874b408ffa1dbf7d53`

## Remaining admin/archive SQLite

SQLite implementations, migration/reconciliation scripts, offline comparator fixtures,
historical backup files and explicit admin/test paths remain. They are not reachable from the
tracked Supabase production configuration. Existing SQLite files were not deleted, moved,
renamed, rebuilt or modified.

## Remaining blockers and final status

1. Windows PowerShell 5.1 startup/UI/READ/WRITE must be run on a Windows device.
2. PC-A → Supabase → PC-B (and preferably reverse) must be run on two genuine PCs.

The completed implementation is checkpointed separately without claiming final Stage5 PASS.
The checkpoint hash and external-machine evidence are recorded in the external acceptance
document. A final acceptance commit is created only after both required checks pass.

**STAGE5 = FAIL (external acceptance pending)**

**FULL SUPABASE RUNTIME MIGRATION = NOT COMPLETE**
