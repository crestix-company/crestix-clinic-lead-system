# Cloud Run READ ONLY Supabase role proposal

Updated: 2026-10-19
Status: investigation and SQL proposal only; no database or Cloud Run changes were made.

## Decision

`clinic_cloudrun_reader` is a reasonable dedicated login for the IAP protected read-only
application, provided its existing role state is accepted and it is tested through the exact
Session Pooler endpoint before release. The desired row scope is all
national clinic rows (`USING (true)`), because IAP is the application access boundary and the
purpose is nationwide browsing/export. This is a broad data scope, so the IAP audience and
application export columns must be reviewed before enabling it.

**Safety result: conditional / not yet ready to apply.** Read-only catalog checks were run
against Supabase project `hwdvyhunozporfuefteb` on 2026-10-19. The existing role is a login,
`INHERIT`, and `NOBYPASSRLS`; no role is a member of it. `public.clinics` is the only
allowlisted object with effective SELECT today, and that grant covers all columns. The other
three required objects have RLS enabled but no effective SELECT or schema USAGE for this role.
The target Cloud Run secret was not inspected, so this project cannot be confirmed as the
deployed Cloud Run target. Do not apply until `INHERIT`, its membership edge to `postgres`, and
the existing broad clinic SELECT are explicitly accepted or separately remediated.

## Revision and target verification

- The latest `origin/main` was fetched for this task; it resolved to `720f986`
  (`Merge pull request #116 ...`). The feature branch was created from that commit.
- Tracked `config/production_runtime.env` selects Supabase for both data and write backends,
  and says credentials are in ignored `.supabase-runtime.env.local`.
- That local credential file is absent. The read-only Supabase catalog connection confirmed
  this named project is queryable, but the Cloud Run secret target remains unverified.
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
| `public.clinics` | Nationwide clinic master, filters, counts, list and fixed-width READ ONLY export | `SELECT` (already present, table-wide) |
| `hp_research.clinic_hp_research` | HP rank, URLs, fetch state and website treatment categories; website summary | `SELECT` |
| `treatment.clinic_treatment_research` | Confirmed treatment categories and category filters | `SELECT` |
| `treatment.clinic_research_status` | Per-clinic treatment research state | `SELECT` |

`public.clinics` supplies clinic/department filters and nationwide counts. HP rank/URL data and
the visible website-research summary use `hp_research.clinic_hp_research`. Treatment categories
and per-clinic treatment status use the two `treatment` tables. C's READ ONLY workflow skips
`store.metrics()`, `listing()` suppresses email status lookups, export-cache signatures bypass
`store.revision()`, and `export_read_only()` reads only clinic projection fields. The separate
Comdesk identity lookup in
`public.sales_entity_clinics` / `public.sales_entity_comdesk_identity` is feature-gated by
`CLINIC_COMMON_IDENTITY_ENABLED`, currently `0` in tracked production routing; do not grant
those objects for the initial release.

### Comparison with C's four-table list

| Group | Tables | Decision |
|---|---|---|
| C core list | `public.clinics`, `hp_research.clinic_hp_research`, `treatment.clinic_treatment_research`, `treatment.clinic_research_status` | Required for nationwide list/filter, HP rank, treatment filter/status, and counts. |
| READ ONLY export | Same four core tables | Fixed-width export, with blank email output and no stored Comdesk row/template reads. |
| Deferred | `provenance.change_history`, `provenance.match_reviews`, `provenance.source_records`, Step 6 Comdesk linkage/history, Email KPI and delivery state, HP Job/research-job tables | No initial grant. READ ONLY UI must bypass these paths. |

The C branch checked was `74047333791bbbf8b8060a986a5c570b69028e31` (PR #117 head at review
time). Live catalog checks confirmed all four objects exist and have RLS enabled. The three
non-clinic objects have no current SELECT for this role. Do not grant
`provenance.comdesk_original_rows`, `provenance.templates`, or `public.clinic_email_enrichment`
for this C implementation.

The C branch bypasses `SupabaseClinicRepository.metrics()`, which reads
`provenance.match_reviews` and `provenance.source_records`, and avoids HP Job, Step 6, Email KPI
and delivery history. Its READ ONLY exporter uses fixed clinic fields and leaves the email
column blank; it does not preserve original Comdesk row/template values. This matches the
four-table allowlist but differs from normal CSV/Excel output. Product owner must approve that
difference, or C must restore those fields before the allowlist is expanded. Normal mode stays
on the existing exporter.

## Out of scope

No grant or policy is proposed for `provenance.*` (including Comdesk original rows/templates,
change history, reviews and source records), `public.clinic_email_enrichment`, Step 6 linkage/
import history, `outreach.*` / email delivery records, Gmail data, Email KPI repositories,
`research.*`, `app_config.*`, `sales_entity_*`, identity sequences, or any write privilege.

## Unapplied SQL proposal for the existing environment

This is a review artifact, not an executable migration. Read-only catalog checks confirmed
tables exist, RLS is enabled, `CONNECT` and `public` USAGE already exist, and no reader-specific
policies exist. Do not change existing roles, policies, or RLS enablement. This adds only schema
USAGE, missing SELECT, and new role-specific SELECT policies. Review findings below before use.

```sql
-- PROPOSAL ONLY. Existing target: no CREATE/ALTER ROLE, membership, password, policy
-- replacement, or RLS enablement is included.
GRANT USAGE ON SCHEMA hp_research, treatment TO clinic_cloudrun_reader;
GRANT SELECT ON TABLE hp_research.clinic_hp_research,
  treatment.clinic_treatment_research,
  treatment.clinic_research_status
TO clinic_cloudrun_reader;
-- Existing role already has table-wide SELECT on public.clinics (all columns).
-- Preserve existing policies. These additions expose all rows to this server-side role;
-- IAP is the end-user boundary.
CREATE POLICY clinic_cloudrun_reader_select ON public.clinics
  FOR SELECT TO clinic_cloudrun_reader USING (true);

CREATE POLICY clinic_cloudrun_reader_select ON hp_research.clinic_hp_research
  FOR SELECT TO clinic_cloudrun_reader USING (true);

CREATE POLICY clinic_cloudrun_reader_select ON treatment.clinic_treatment_research
  FOR SELECT TO clinic_cloudrun_reader USING (true);

CREATE POLICY clinic_cloudrun_reader_select ON treatment.clinic_research_status
  FOR SELECT TO clinic_cloudrun_reader USING (true);
```

Immediately before approved execution, check for policy-name conflicts again. If a name exists,
stop for review; never replace or drop it. Do not use `FORCE ROW LEVEL SECURITY`.

### New environment reference only

The role already exists on the reviewed target. This reference is only for a genuinely new
environment; do not run against this project:

```sql
CREATE ROLE clinic_cloudrun_reader
  LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
```

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
- Live catalog confirms that `clinic_cloudrun_reader` exists. It is LOGIN, INHERIT, and
  NOBYPASSRLS; has no inbound memberships; and has the unusual edge `postgres` is a member of
  `clinic_cloudrun_reader`. No role or membership was changed.
- `public.clinics` SELECT is a direct grant by `postgres` to the reader, with no grant option;
  it is table-level (all columns). No explicit column ACLs were found.
- Catalog policies: `public.clinics` has existing SELECT policies for `clinic_email_target_reader`
  and `clinic_readonly`, plus `clinic_runtime_rw` FOR ALL; HP research has SELECT policies for
  `clinic_email_target_reader` and `clinic_readonly`, plus `clinic_runtime_rw` FOR ALL. Both
  treatment tables have `clinic_runtime_rw` FOR ALL. All these are for other roles. The reader
  policies are new and additive.
- The live `clinic_email_enrichment` table had zero rows at check time, so status distribution
  cannot validate the verified-email row predicate. The table is excluded from initial access.
- Add policies only for `clinic_cloudrun_reader`; do not drop, replace, or widen existing
  policies. The four targets already have RLS enabled. Any other PUBLIC or inherited privileges
  must be reviewed separately before production use.
- Live check found SELECT only on `public.clinics` (all columns) and no table write privileges
  in public/hp_research/treatment/provenance. Required tables have RLS enabled, FORCE RLS off,
  and no policy targeting this reader. Existing role is INHERIT (no incoming memberships) and
  has TEMP; resolve acceptance of those states before enabling production use.

## Session Pooler assessment

The code uses `psycopg.connect()` with the configured URL and does not depend on session-only
prepared state. Existing project documentation records successful direct login as a restricted
runtime identity over the Supabase Session Pooler, and the read-only connector explicitly sends
`SET default_transaction_read_only = on` after connecting because poolers may ignore libpq
startup options. This supports compatibility in principle. It is not validation of
`clinic_cloudrun_reader` or the target endpoint: no credential was available for testing (and
none was displayed). Before release, test through the exact target
Session Pooler URL: authentication, `current_user`, `session_user`, `SHOW
default_transaction_read_only`, `SELECT 1`, representative nationwide query and CSV query;
then confirm INSERT/UPDATE/DELETE/DDL are denied. Never print the URL.

## A担当 handoff / approvals

1. C/A: retain the audit/Email KPI/HP Job/Step 6 bypass and confirm the four-table READ ONLY
   allowlist. Product owner must approve the blank-email, fixed-field READ ONLY export versus
   normal Comdesk export. Confirm normal mode remains unchanged.
2. Confirm the deployed Cloud Run secret points to project `hwdvyhunozporfuefteb` and a
   Session Pooler URL, without exposing its value.
3. DBA/product owner must accept or separately remediate existing INHERIT, the postgres
   membership edge, table-wide clinic SELECT, and TEMP. No role change is included here.
4. Approve exact GRANT and new policy statements, then validate with the dedicated role in an
   isolated database and run read/write-denial checks on the approved target before Cloud Run.

No GRANT, RLS change, migration, data update, role membership change, password change, Cloud Run
operation, Gmail operation, or PR merge was performed for this review.
