# Cloud Run READ ONLY Supabase role proposal

Updated: 2026-10-19  
Status: investigation and SQL proposal only; no database or Cloud Run changes were made.

## Decision

`clinic_cloudrun_reader` is a reasonable dedicated login for the IAP protected read-only
application, provided it is a new, non-inheriting, non-owner, `NOBYPASSRLS` login and is
tested through the exact Session Pooler endpoint before release. The desired row scope is all
national clinic rows (`USING (true)`), because IAP is the application access boundary and the
purpose is nationwide browsing/export. This is a broad data scope, so the IAP audience and
application export columns must be reviewed before enabling it.

**Safety result: conditional / not yet ready to apply.** The app's READ ONLY flag sets
`default_transaction_read_only=on`, and the code's connection setup issues that setting after
connecting, which covers poolers that ignore startup options. However, the actual Cloud Run
secret target and current DB catalog were not available for inspection. The application-side
READ ONLY path now avoids internal provenance tables; its dedicated reader login and its
privileges still require isolated and target-side verification.

## Revision and target verification

- The latest `origin/main` was fetched for this task; it resolved to `720f986`
  (`Merge pull request #116 ...`). The feature branch was created from that commit.
- Tracked `config/production_runtime.env` selects Supabase for both data and write backends,
  and says credentials are in ignored `.supabase-runtime.env.local`.
- That local credential file is absent. Cloud Run/Secret Manager inspection is prohibited by
  the task. Therefore project ref `hwdvyhunozporfuefteb` cannot be confirmed as the deployed
  Cloud Run target. It is the named target for this review only.
- Do not point the app at the existing `clinic_runtime` login: repository records show it has
  application write privileges. Provision/use the dedicated reader URL only after approval.

## READ ONLY execution path and minimum objects

`Dockerfile.cloudrun` describes `app_v2.py` behind IAP with `CLINIC_READ_ONLY_UI=true`.
That mode limits navigation to `かんたん操作` and `営業対象・出力`, skips startup writes,
and disables write/job controls. The READ ONLY routes now use a clinic projection overview
and a dedicated narrow CSV exporter; they do not run job, Comdesk-history, audit, or email
lookups. Reads and CSV generation use `SupabaseRuntimeStore` and `SupabaseClinicRepository`.

| Object | Why the READ ONLY path uses it | Proposed access |
|---|---|---|
| `public.clinics` | Nationwide clinic master, departments, prefecture, phone/address, UUID, effective JSON, current/HP fields; filters, counts and export | `SELECT` |
| `hp_research.clinic_hp_research` | Machine HP rank, URLs/final URL, fetch state and detected website treatment categories; merged into list/export and HP summary | `SELECT` |
| `treatment.clinic_treatment_research` | Confirmed treatment categories, category options and per-clinic treatment status | `SELECT` |
| `treatment.clinic_research_status` | Completes per-clinic treatment-status display (`DONE_NO_CATEGORY`, failures, not researched) | `SELECT` |

`public.clinics` is the source for clinic/department filters. HP rank is synthesized from
`public.clinics.hp_rank` and `hp_research.clinic_hp_research.machine_hp_rank`. Treatment
categories come from `treatment.clinic_treatment_research`; both the current confirmed
categories and the clinic research status table are used by the list UI. CSV export uses the
clinic and HP records. The separate Comdesk identity lookup in
`public.sales_entity_clinics` / `public.sales_entity_comdesk_identity` is feature-gated by
`CLINIC_COMMON_IDENTITY_ENABLED`, currently `0` in tracked production routing; do not grant
those objects for the initial release.

The code and focused tests establish the initial READ ONLY route bypasses
`SupabaseClinicRepository.metrics()`, which reads
`provenance.match_reviews` and `provenance.source_records`. It also avoids HP job state,
Comdesk linkage/history, `provenance.change_history`, Comdesk source rows/templates, and all
email repositories/enrichment. The READ ONLY export uses the existing fixed Comdesk column
contract; its email column remains blank. Normal mode retains the original exporter and
operational UI.

## Out of scope

No grant or policy is proposed for `provenance.*` (including change history, import history,
reviews, audit records, templates), `outreach.*` / email delivery records, Gmail data,
`research.*`, `app_config.*`, `sales_entity_*`, identity sequences, or any write privilege.
Those objects are not necessary for the nationwide clinic filter and dedicated READ ONLY CSV
path. The general-purpose export remains available in normal mode only.

## Unapplied SQL proposal

This is a review artifact, not an executable migration. Confirm schema/table existence, current
RLS state, inherited grants, default privileges and role state against the approved target
before adapting it. Role password provisioning must happen separately through the approved
secret manager process; no password is included here.

```sql
-- PROPOSAL ONLY. Do not run as part of this review.
-- Create a new login without membership in any other role. Set its password separately
-- through the approved credential workflow; do not reuse clinic_runtime credentials.
CREATE ROLE clinic_cloudrun_reader
  LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;

ALTER ROLE clinic_cloudrun_reader SET default_transaction_read_only = on;
ALTER ROLE clinic_cloudrun_reader SET statement_timeout = '60s';

GRANT CONNECT ON DATABASE postgres TO clinic_cloudrun_reader;
GRANT USAGE ON SCHEMA public, hp_research, treatment TO clinic_cloudrun_reader;
GRANT SELECT ON TABLE public.clinics,
  hp_research.clinic_hp_research,
  treatment.clinic_treatment_research,
  treatment.clinic_research_status
TO clinic_cloudrun_reader;

-- Apply only if each table is confirmed RLS-enabled and no existing safe policy applies.
-- Policies expose every row to this server-side role; IAP is the user boundary.
ALTER TABLE public.clinics ENABLE ROW LEVEL SECURITY;
CREATE POLICY clinic_cloudrun_reader_select ON public.clinics
  FOR SELECT TO clinic_cloudrun_reader USING (true);

ALTER TABLE hp_research.clinic_hp_research ENABLE ROW LEVEL SECURITY;
CREATE POLICY clinic_cloudrun_reader_select ON hp_research.clinic_hp_research
  FOR SELECT TO clinic_cloudrun_reader USING (true);

ALTER TABLE treatment.clinic_treatment_research ENABLE ROW LEVEL SECURITY;
CREATE POLICY clinic_cloudrun_reader_select ON treatment.clinic_treatment_research
  FOR SELECT TO clinic_cloudrun_reader USING (true);

ALTER TABLE treatment.clinic_research_status ENABLE ROW LEVEL SECURITY;
CREATE POLICY clinic_cloudrun_reader_select ON treatment.clinic_research_status
  FOR SELECT TO clinic_cloudrun_reader USING (true);
```

Do not run the `ALTER TABLE ... ENABLE ROW LEVEL SECURITY` / `CREATE POLICY` blindly: existing
RLS may already be enabled and policy names may conflict. Inspect catalog first and use reviewed,
idempotent statements in an approved migration. Do not use `FORCE ROW LEVEL SECURITY` without
checking table ownership and service roles.

## RLS, inheritance and write-safety audit status

- Repository migration `scripts/supabase_migration/stage4d_runtime_write_schema.sql` documents
  `clinic_runtime` as a broad runtime role with `clinic_runtime_rw` (`FOR ALL`) policies across
  write-path tables. It is not suitable for this Cloud Run reader.
- Historical design docs say mapped runtime tables had RLS enabled and role-specific policies,
  but that is not a current catalog query for this proposed target or new role.
- No current `clinic_cloudrun_reader` role, grants, memberships, RLS policies, or write grants
  could be verified. No database connection was made.
- For the new role, acceptance criteria are: `rolsuper=false`, `rolbypassrls=false`,
  `rolcreatedb=false`, `rolcreaterole=false`; no role memberships; no table/sequence write
  grants; no ownership; `has_table_privilege(..., 'INSERT'/'UPDATE'/'DELETE') = false` for
  every object; RLS enabled and only intended SELECT policies; SELECT returns the complete
  national population. Verify attempted writes are denied in a disposable isolated database.

## Session Pooler assessment

The code uses `psycopg.connect()` with the configured URL and does not depend on session-only
prepared state. Existing project documentation records successful direct login as a restricted
runtime identity over the Supabase Session Pooler, and the read-only connector explicitly sends
`SET default_transaction_read_only = on` after connecting because poolers may ignore libpq
startup options. This supports compatibility in principle. It is not a validation of
`clinic_cloudrun_reader` or of the target endpoint: the required credential file is absent and
the role does not have verified credentials. Before release, test through the exact target
Session Pooler URL: authentication, `current_user`, `session_user`, `SHOW
default_transaction_read_only`, `SELECT 1`, representative nationwide query and CSV query;
then confirm INSERT/UPDATE/DELETE/DDL are denied. Never print the URL.

## A担当 handoff / approvals

1. Confirm the deployed Cloud Run secret points to project `hwdvyhunozporfuefteb` and a
   Session Pooler URL, without exposing its value.
2. Have DBA inspect current target catalog and role name availability, then approve the exact
   role/grant/RLS statements and dedicated credential provisioning.
3. Validate with the dedicated role in an isolated database, then run read and write-denial
   checks on the approved target before Cloud Run configuration is separately approved.

No GRANT, RLS change, migration, data update, role membership change, password change, Cloud Run
operation, Gmail operation, or PR merge was performed for this review.
