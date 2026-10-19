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
`default_transaction_read_only=on`, and the connection setup issues that setting after
connecting, which covers poolers that ignore startup options. However, the actual Cloud Run
secret target and current DB catalog were not available for inspection. The proposal below
keeps the ordinary sales CSV/Excel data contract, including original Comdesk row values and
the verified email output column. It excludes audit/history aggregates, delivery records,
Email KPIs, and HP Job access. The dedicated reader login and its privileges still require
isolated and target-side verification.

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
and a dedicated READ ONLY CSV exporter; they do not run job, Comdesk-history, audit, or Email
KPI lookups. Reads and CSV generation use `SupabaseRuntimeStore` and `SupabaseClinicRepository`.

| Object | Why the READ ONLY path uses it | Proposed access |
|---|---|---|
| `public.clinics` | Nationwide clinic master, departments, prefecture, phone/address, UUID, effective JSON and current/HP fields; filters, counts, rows and export | `SELECT` |
| `hp_research.clinic_hp_research` | Machine HP rank, URLs/final URL, fetch state and detected website treatment categories; list, HP summary and export | `SELECT` |
| `treatment.clinic_treatment_research` | Confirmed treatment categories, category options, filters and per-clinic treatment status | `SELECT` |
| `treatment.clinic_research_status` | Completes per-clinic treatment-status display (`DONE_NO_CATEGORY`, failures, not researched) | `SELECT` |
| `provenance.comdesk_original_rows` | Ordinary export preserves the source Comdesk row when one exists | `SELECT` |
| `provenance.templates` | Ordinary export uses the stored source-column mapping for original Comdesk rows | `SELECT` |
| `public.clinic_email_enrichment` | Ordinary export appends the verified-email output column; this is current enrichment data, not delivery history or an Email KPI | `SELECT` |

`public.clinics` is the source for clinic/department filters. HP rank is synthesized from
`public.clinics.hp_rank` and `hp_research.clinic_hp_research.machine_hp_rank`. Treatment
categories come from `treatment.clinic_treatment_research`; both the current confirmed
categories and the clinic research status table are used by the list UI. The ordinary sales
CSV/Excel path calls `SupabaseRuntimeStore.export()`: `_export_records()` reads
`provenance.comdesk_original_rows` and `provenance.templates`, while
`_attach_export_metadata()` reads `public.clinic_email_enrichment`. These three are needed to
preserve ordinary export values and columns; they are not Step 6 synchronization or audit
tables. `provenance.change_history` is only read by `store.revision()` for the export cache
signature, not to construct the output. The READ ONLY UI should omit that lookup while keeping
the ordinary export path. The separate Comdesk identity lookup in
`public.sales_entity_clinics` / `public.sales_entity_comdesk_identity` is feature-gated by
`CLINIC_COMMON_IDENTITY_ENABLED`, currently `0` in tracked production routing; do not grant
those objects for the initial release.

### Comparison with C's four-table list

| Group | Tables | Decision |
|---|---|---|
| C core list | `public.clinics`, `hp_research.clinic_hp_research`, `treatment.clinic_treatment_research`, `treatment.clinic_research_status` | Required for nationwide list/filter, HP rank, treatment filter/status, and counts. |
| Ordinary sales export compatibility | `provenance.comdesk_original_rows`, `provenance.templates`, `public.clinic_email_enrichment` | Add read access only for the standard CSV/Excel path. Restrict SELECT columns on these objects to those selected by the exporter. |
| Deferred | `provenance.change_history`, `provenance.match_reviews`, `provenance.source_records`, Step 6 Comdesk linkage/history, Email KPI and delivery state, HP Job/research-job tables | No initial grant. READ ONLY UI must bypass these paths. |

This allowlist is derived from code, not a confirmation of live table existence, current RLS,
existing grants, or the deployed project's migration state.

The C branch's READ ONLY route bypasses `SupabaseClinicRepository.metrics()`, which reads
`provenance.match_reviews` and `provenance.source_records`, and avoids HP job state, Step 6
Comdesk linkage/history, Email KPI repositories and delivery history. For the current C
implementation to satisfy this publication policy, its READ ONLY exporter must preserve the
ordinary CSV/Excel row values and verified-email column by using the three explicitly allowed
export dependencies above, while skipping `store.revision()` in READ ONLY mode. C's present
`export_read_only()` intentionally omits source rows/templates and leaves email blank; that
does not meet the current requirement to preserve ordinary sales export output. Normal mode
must remain on its existing path.

Suggested C-side implementation is to keep `simple_sales_ui()`'s READ ONLY branch and
`store.revision()` bypass, but have `SupabaseRuntimeStore.export_read_only()` build its rows
with `_export_records(records)` and `_attach_export_metadata(records, rows)`, just like
`export()`. That method does not need `provenance.change_history`; its only extra reads are the
approved export dependencies. Then compare READ ONLY and normal export headers/rows for the
same fixture and assert no query touches the deferred tables.

## Out of scope

No grant or policy is proposed for `provenance.change_history`,
`provenance.match_reviews`, `provenance.source_records`, Step 6 linkage/import history,
`outreach.*` / email delivery records, Gmail data, Email KPI repositories, `research.*`,
`app_config.*`, `sales_entity_*`, identity sequences, or any write privilege. The only
`provenance` grants are for the original Comdesk row/template reads needed by the ordinary
sales export. No privilege is granted on email delivery state; `public.clinic_email_enrichment`
contains verified clinic contact enrichment and is limited to the export column.

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
GRANT USAGE ON SCHEMA public, hp_research, treatment, provenance TO clinic_cloudrun_reader;
GRANT SELECT ON TABLE public.clinics,
  hp_research.clinic_hp_research,
  treatment.clinic_treatment_research,
  treatment.clinic_research_status
TO clinic_cloudrun_reader;
GRANT SELECT (id, clinic_id, template_id, row_json, uuid)
  ON TABLE provenance.comdesk_original_rows TO clinic_cloudrun_reader;
GRANT SELECT (id, headers_json, mapping_json)
  ON TABLE provenance.templates TO clinic_cloudrun_reader;
GRANT SELECT (clinic_id, email, status, verified_on_official)
  ON TABLE public.clinic_email_enrichment TO clinic_cloudrun_reader;

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

ALTER TABLE provenance.comdesk_original_rows ENABLE ROW LEVEL SECURITY;
CREATE POLICY clinic_cloudrun_reader_select ON provenance.comdesk_original_rows
  FOR SELECT TO clinic_cloudrun_reader USING (true);

ALTER TABLE provenance.templates ENABLE ROW LEVEL SECURITY;
CREATE POLICY clinic_cloudrun_reader_select ON provenance.templates
  FOR SELECT TO clinic_cloudrun_reader USING (true);

ALTER TABLE public.clinic_email_enrichment ENABLE ROW LEVEL SECURITY;
CREATE POLICY clinic_cloudrun_reader_select ON public.clinic_email_enrichment
  FOR SELECT TO clinic_cloudrun_reader
  USING (status = 'VERIFIED_EMAIL' AND verified_on_official = true);
```

Do not run the `ALTER TABLE ... ENABLE ROW LEVEL SECURITY` / `CREATE POLICY` blindly. Confirm
each table's current RLS flag and policy names first. If RLS is already enabled, add only the
reader-specific SELECT policy after confirming it does not exist; do not drop or replace any
existing policy. If RLS is disabled, stop and assess the effect on every current role before
enabling it. Do not use `FORCE ROW LEVEL SECURITY` without checking ownership and service roles.

## RLS, inheritance and write-safety audit status

- Repository migration `scripts/supabase_migration/stage4d_runtime_write_schema.sql` documents
  `clinic_runtime` as a broad runtime role with `clinic_runtime_rw` (`FOR ALL`) policies across
  write-path tables, including some read-path tables. The email-enrichment SELECT design also
  grants `clinic_runtime` access. Those grants and policies belong to the existing application
  role; do not reuse or alter it for this Cloud Run reader.
- The repository migration states its 19 runtime tables had RLS enabled, including the four
  core tables and `provenance.templates` / `provenance.comdesk_original_rows`. The checked-in
  email-enrichment reader SQL adds a role-specific policy but does not itself establish the
  current live RLS state. Treat all these as historical repository evidence, not live catalog
  verification.
- Historical design docs say mapped runtime tables had RLS enabled and role-specific policies,
  but that is not a current catalog query for this proposed target or new role.
- No current `clinic_cloudrun_reader` role, grants, memberships, RLS policies, or write grants
  could be verified. No database connection was made.
- The verified-email CSV column exposes current clinic contact addresses to every IAP-authorized
  user who can download the standard export. Confirm this is approved; if it is not, the output
  contract must be explicitly narrowed rather than silently blanking the column.
- Add policies only for `clinic_cloudrun_reader`; do not drop, replace, or widen existing
  policies. If any target table currently has RLS disabled, inspect its existing access paths
  before enabling RLS because enabling it changes behavior for every role on that table. If
  PUBLIC or inherited role grants provide out-of-scope access, stop: resolving that requires a
  separate approved change, not a change to other roles in this proposal.
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

1. C: retain `SupabaseClinicRepository.metrics()` bypass, disabled Email KPI/HP Job/Step 6
   paths, and no audit table reads; update `export_read_only()` to preserve ordinary Comdesk
   source-row/template behavior and verified-email column; skip only `store.revision()` in
   READ ONLY mode. Confirm normal mode remains unchanged.
2. Confirm the deployed Cloud Run secret points to project `hwdvyhunozporfuefteb` and a
   Session Pooler URL, without exposing its value.
3. Have DBA inspect current target catalog and role name availability, then approve the exact
   role/grant/RLS statements and dedicated credential provisioning.
4. Validate with the dedicated role in an isolated database, then run read and write-denial
   checks on the approved target before Cloud Run configuration is separately approved.

No GRANT, RLS change, migration, data update, role membership change, password change, Cloud Run
operation, Gmail operation, or PR merge was performed for this review.
