# Cloud Run READ ONLY Supabase role proposal

Updated: 2026-10-19
Status: investigation and SQL proposal only; no database or Cloud Run changes were made.

## Decision

`clinic_cloudrun_reader` is an existing login for the IAP-protected read-only application. It
must not be recreated or altered. Its current role state must be reviewed and its connection
tested through the exact Session Pooler endpoint before release. The desired row scope is all
national clinic rows (`USING (true)`), because IAP is the application access boundary and the
purpose is nationwide browsing/export. This is a broad data scope, so the IAP audience and
application export columns must be reviewed before enabling it.

**Safety result: NO-GO for DB application pending separate approval and remediation.** Read-only catalog checks were run
against Supabase project `hwdvyhunozporfuefteb` on 2026-10-19. The existing role is a login,
`INHERIT`, and `NOBYPASSRLS`; it is not a member of any other role. `public.clinics` is the only
allowlisted object with effective SELECT today, and that grant covers all columns. The other
six required objects have RLS enabled but no effective SELECT for this role.
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
and disables write/job controls. The intended READ ONLY routes use a clinic projection overview
and a READ ONLY standard CSV/Excel exporter. PR #119 head reviewed is
`fef6b9f430b19e34dde2b41c97329ab333302ee1` (not the superseded PR #117 implementation).
It retains standard Comdesk output data and columns and adds three export dependencies to the
four browse tables.

| Object | Why the READ ONLY path uses it | Proposed access |
|---|---|---|
| `public.clinics` | Nationwide clinic master, filters, counts, list and fixed-width READ ONLY export | `SELECT` (already present, table-wide) |
| `hp_research.clinic_hp_research` | HP rank, URLs, fetch state and website treatment categories; website summary | `SELECT` |
| `treatment.clinic_treatment_research` | Confirmed treatment categories and category filters | `SELECT` |
| `treatment.clinic_research_status` | Per-clinic treatment research state | `SELECT` |
| `provenance.comdesk_original_rows` | Preserve original Comdesk values/UUID for standard export | Column SELECT: `id, clinic_id, template_id, row_json, uuid` |
| `provenance.templates` | Restore original source-column order/mapping | Column SELECT: `id, headers_json, mapping_json` |
| `public.clinic_email_enrichment` | Append officially verified clinic contact email in standard export (approved for IAP users) | Column SELECT: `clinic_id, email, status, verified_on_official`; RLS restricts visible rows |

Column scope for the other four tables: `public.clinics` remains all existing columns under its
direct table-level grant (`id, uuid, clinic_name, phone, address, phone_norm, name_norm,
name_prefix, address_norm, medical_key, prefecture, medical_type, base_json, effective_json,
active, designation_date, recent_until, registration_reason, owner_equal, age_probability,
hp_status, hp_url, hp_rank, signal_count, hot_status, departments_json, treatments_json,
signals_json, first_seen_at, last_seen_at, source_as_of_date, is_new, merged_into,
tel_match_key, merge_hold, maps_presence_status, maps_profile_url, maps_website_url,
maps_match_method, maps_checked_at, exclude_reason`). The additional grants are restricted to
`hp_research.clinic_hp_research(clinic_id, machine_hp_rank, fetch_status, hp_url, final_url,
treatment_categories)`, `treatment.clinic_treatment_research(clinic_id, research_status,
treatment_category_name)`, and `treatment.clinic_research_status(clinic_id, research_status)`.

`public.clinics` supplies clinic/department filters, search and counts. HP research supplies rank,
URL, website summary, and website treatment detection. Treatment tables supply confirmed
categories and per-clinic treatment status. In PR #119, `export_read_only()` deliberately calls
the same `_export_records()` and `_attach_export_metadata()` as normal export. This reads
Comdesk original rows and template mapping; the email query selects only rows with
`status='VERIFIED_EMAIL' AND verified_on_official=true`. `revision()` is bypassed in READ ONLY
mode, so `provenance.change_history` is not needed. The separate Comdesk identity lookup in
`public.sales_entity_clinics` / `public.sales_entity_comdesk_identity` is feature-gated by
`CLINIC_COMMON_IDENTITY_ENABLED`, currently `0` in tracked production routing; do not grant
those objects for the initial release.

### Comparison with the superseded four-table design

| Group | Tables | Decision |
|---|---|---|
| Browse and filters | `public.clinics`, `hp_research.clinic_hp_research`, `treatment.clinic_treatment_research`, `treatment.clinic_research_status` | Required, unchanged. |
| Standard CSV/Excel compatibility | `provenance.comdesk_original_rows`, `provenance.templates` | Required by PR #119 to preserve original Comdesk values and mapping. |
| Verified-email export | `public.clinic_email_enrichment` | Required and specification approved for IAP-authorized CSV/Excel. Database application remains unapproved; sending, delivery history and Email KPI are excluded. |
| Deferred | `provenance.change_history`, `provenance.match_reviews`, `provenance.source_records`, Step 6 Comdesk linkage/history, Email KPI and delivery state, HP Job/research-job tables | No initial grant. READ ONLY UI must bypass these paths. |

PR #119 head was fetched and reviewed. The export tests verify identical READ ONLY and normal
CSV/Excel bytes for a fixture, Comdesk source values and mapping, verified email selection,
no writes, and no change-history/audit/HP-job/Step6 table references in the export path. Its
read-only treatment-filter branch skips `research.research_results` and reads confirmed
categories from `treatment.clinic_treatment_research` instead. The older 4-table design came
from PR #117 and is superseded.

Focused validation on PR #119 head: 112 tests passed across READ ONLY export/isolation and
treatment-filter suites. These use fixtures/mocks; they do not authenticate as the live reader
or prove nationwide row counts through the Session Pooler.

PR #119's READ ONLY workflow bypasses `metrics()` and email statuses, does not call HP Job,
Step 6, or change-history paths, and uses the standard exporter for source row/template mapping
and verified email. The verified-email filter is exact in both SQL and `_verified_email_map()`.
The owner has approved output of official-HP-confirmed contact emails to CSV/Excel for
IAP-authorized internal users. This approves the data-use specification only; it does not
authorize database privilege application or deployment. Sending, delivery history and Email KPI
remain out of scope.

### PR #119/#120 integrated verification (2026-10-19)

A担当 reports completing a temporary integration verification at commit
`a7d581025afaa37ce807748cc5aee3b55b94f444`. According to that verification, the PR #119
READ ONLY-specific query/count/export paths, Gmail draft save guards, and READ ONLY export tests
are retained; captured SQL stays within the seven-table allowlist and does not reference audit,
history, Email KPI, HP Job, or Step 6 tables. The focused suite passed 24 tests and full pytest
passed 2201 tests with 55 skipped.

This supersedes the earlier assessment that the PR #119/#120 integration removed those
protections. The seven-table permissions proposal above remains unchanged. These are application
integration test results only: they do not validate live RLS visibility, the production role's
effective grants, nationwide row counts, Session Pooler authentication, or Cloud Run's deployed
database target. Production DB readiness therefore remains subject to the live-connection and
catalog blockers below.

## Out of scope

No grant or policy is proposed for `provenance.change_history`, `provenance.match_reviews`,
`provenance.source_records`, HP Job/research-job tables, Step 6 linkage/import history,
`outreach.*` / delivery records, Email KPI repositories, Gmail data, `research.*` (including
`research.research_results`), `app_config.*`, `sales_entity_*`, sequences, or any write privilege.

## Unapplied SQL proposal for the existing environment

This is a review artifact, not an executable migration. Read-only catalog checks confirmed the
seven tables exist and have RLS enabled; `CONNECT` and `public` USAGE already exist. Do not alter
role attributes/memberships, existing CRM policies, or RLS enablement. Six-table SQL is shown
first. Email output use is approved, but its SQL block still requires separate database-change approval.

```sql
-- PROPOSAL ONLY. Existing target: no CREATE/ALTER ROLE, membership, password, policy
-- replacement, or RLS enablement is included.
GRANT USAGE ON SCHEMA hp_research, treatment, provenance TO clinic_cloudrun_reader;
GRANT SELECT (clinic_id, machine_hp_rank, fetch_status, hp_url, final_url, treatment_categories)
  ON TABLE hp_research.clinic_hp_research TO clinic_cloudrun_reader;
GRANT SELECT (clinic_id, research_status, treatment_category_name)
  ON TABLE treatment.clinic_treatment_research TO clinic_cloudrun_reader;
GRANT SELECT (clinic_id, research_status)
  ON TABLE treatment.clinic_research_status TO clinic_cloudrun_reader;
GRANT SELECT (id, clinic_id, template_id, row_json, uuid)
  ON TABLE provenance.comdesk_original_rows TO clinic_cloudrun_reader;
GRANT SELECT (id, headers_json, mapping_json)
  ON TABLE provenance.templates TO clinic_cloudrun_reader;
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

CREATE POLICY clinic_cloudrun_reader_select ON provenance.comdesk_original_rows
  FOR SELECT TO clinic_cloudrun_reader USING (true);

CREATE POLICY clinic_cloudrun_reader_select ON provenance.templates
  FOR SELECT TO clinic_cloudrun_reader
  USING (EXISTS (
    SELECT 1 FROM provenance.comdesk_original_rows original
    WHERE original.template_id = templates.id
  ));
```

`public.clinics` already has a direct table-level SELECT grant to this role, so all its 41
catalog columns remain accessible. This exceeds the UI projection: `base_json`,
`registration_reason`, `exclude_reason`, Maps provenance fields and other unused source fields
are not needed to render/export these screens. The adapter also needs columns used in search,
filter predicates and sorting. A precise replacement allowlist must be generated and tested
against every PR #119 SQL expression. Since column-level replacement requires REVOKE of the
existing table grant, that separate privilege change remains a least-privilege blocker and is
not included here. Other table grants are limited to columns read by the browse/filter and
export paths. The provenance column grants are the exact columns referenced by the exporter;
`id` is also needed for the `ORDER BY` on original rows. PR #119 currently selects all
templates, but the proposed templates RLS policy limits visible rows to template IDs referenced
by stored Comdesk rows. It depends on the reader policy/grant on `comdesk_original_rows`.

Email output is **specification-approved; database application is not approved**. The following
is part of the proposed seven-table delta, but must remain unapplied until separate DB approval:

```sql
-- APPROVED USE SPECIFICATION; NOT APPROVED FOR EXECUTION in this task.
GRANT SELECT (clinic_id, email, status, verified_on_official)
  ON TABLE public.clinic_email_enrichment TO clinic_cloudrun_reader;
CREATE POLICY clinic_cloudrun_reader_select ON public.clinic_email_enrichment
  FOR SELECT TO clinic_cloudrun_reader
  USING (status = 'VERIFIED_EMAIL'
         AND verified_on_official IS TRUE
         AND NULLIF(BTRIM(email), '') IS NOT NULL);
```

This RLS predicate is a safe narrowing of PR #119's SQL filter
(`status='VERIFIED_EMAIL' AND verified_on_official=true`); `_verified_email_map()` also drops
blank addresses and deduplicates case-insensitively. Thus only nonempty official verified
addresses are exposed/output. At the last live check the table had zero rows, so the current
status distribution and match counts cannot be validated. Enrichment evidence/source columns
are not granted. Validate in a disposable isolated database with fixtures: valid status +
official=true + nonblank email must be visible; wrong status, false/null official flag, and
blank/whitespace/null email must be hidden. Connect as the reader to assert visibility, test
export output and row alignment, and assert writes fail. Never seed fixtures in production.

Immediately before approved execution, check for policy-name conflicts again. If a name exists,
stop for review; never replace or drop it. Do not use `FORCE ROW LEVEL SECURITY`.

### Pre-apply catalog checks

Run these SELECT-only checks immediately before a separately approved application. They inspect
the existing role, membership direction, effective privileges, RLS flags and policy-name
conflicts. Do not include password or credential queries.

```sql
SELECT rolname, rolcanlogin, rolinherit, rolsuper, rolbypassrls,
       rolcreatedb, rolcreaterole,
       has_database_privilege(oid, current_database(), 'CONNECT') AS can_connect,
       has_database_privilege(oid, current_database(), 'TEMP') AS can_temp
FROM pg_roles WHERE rolname = 'clinic_cloudrun_reader';

SELECT parent.rolname AS granted_role, member.rolname AS member_role, m.admin_option
FROM pg_auth_members m
JOIN pg_roles parent ON parent.oid = m.roleid
JOIN pg_roles member ON member.oid = m.member
WHERE parent.rolname = 'clinic_cloudrun_reader'
   OR member.rolname = 'clinic_cloudrun_reader';

-- TEMP/CONNECT source: PUBLIC or explicit database ACLs.
SELECT COALESCE(grantee.rolname, 'PUBLIC') AS grantee, x.privilege_type, x.is_grantable
FROM pg_database d
CROSS JOIN LATERAL aclexplode(COALESCE(d.datacl, acldefault('d', d.datdba))) x
LEFT JOIN pg_roles grantee ON grantee.oid = x.grantee
WHERE d.datname = current_database()
  AND x.privilege_type IN ('CONNECT', 'TEMPORARY', 'CREATE')
ORDER BY 1, 2;

SELECT n.nspname,
       has_schema_privilege('clinic_cloudrun_reader', n.oid, 'USAGE') AS can_use,
       has_schema_privilege('clinic_cloudrun_reader', n.oid, 'CREATE') AS can_create
FROM pg_namespace n
WHERE n.nspname IN ('public','hp_research','treatment','provenance','extensions','research','outreach')
ORDER BY 1;

SELECT n.nspname, c.relname, c.relrowsecurity, c.relforcerowsecurity,
       has_table_privilege('clinic_cloudrun_reader', c.oid, 'SELECT') AS table_select,
       has_table_privilege('clinic_cloudrun_reader', c.oid, 'INSERT') AS can_insert,
       has_table_privilege('clinic_cloudrun_reader', c.oid, 'UPDATE') AS can_update,
       has_table_privilege('clinic_cloudrun_reader', c.oid, 'DELETE') AS can_delete
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE (n.nspname, c.relname) IN
  (('public','clinics'), ('hp_research','clinic_hp_research'),
   ('treatment','clinic_treatment_research'), ('treatment','clinic_research_status'),
   ('provenance','comdesk_original_rows'), ('provenance','templates'),
   ('public','clinic_email_enrichment'))
ORDER BY 1, 2;

SELECT n.nspname, c.relname,
       ARRAY(SELECT a.attname::text FROM pg_attribute a
             WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
               AND has_column_privilege('clinic_cloudrun_reader', c.oid, a.attnum, 'SELECT')
             ORDER BY a.attnum) AS select_columns
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE (n.nspname, c.relname) IN
  (('public','clinics'), ('hp_research','clinic_hp_research'),
   ('treatment','clinic_treatment_research'), ('treatment','clinic_research_status'),
   ('provenance','comdesk_original_rows'), ('provenance','templates'),
   ('public','clinic_email_enrichment'))
ORDER BY 1, 2;

SELECT schemaname, tablename, policyname, roles, cmd, qual, with_check
FROM pg_policies
WHERE (schemaname, tablename) IN
  (('public','clinics'), ('hp_research','clinic_hp_research'),
   ('treatment','clinic_treatment_research'), ('treatment','clinic_research_status'),
   ('provenance','comdesk_original_rows'), ('provenance','templates'),
   ('public','clinic_email_enrichment'))
ORDER BY 1, 2, 3;

-- Email is a privacy approval gate; this returns only counts, never email values.
SELECT count(*) AS total_rows,
       count(*) FILTER (WHERE status = 'VERIFIED_EMAIL'
                        AND verified_on_official IS TRUE
                        AND NULLIF(BTRIM(email), '') IS NOT NULL) AS policy_visible_rows,
       count(*) FILTER (WHERE status IS DISTINCT FROM 'VERIFIED_EMAIL'
                        OR verified_on_official IS DISTINCT FROM TRUE
                        OR NULLIF(BTRIM(email), '') IS NULL) AS policy_hidden_rows
FROM public.clinic_email_enrichment;
```

### Post-apply verification SQL

Run catalog checks as an authorized verifier, then run the final block through the exact
Session Pooler using the reader credential (never print the URL). The database changes remain
unapplied by this PR.

```sql
-- Expected effective write privileges: false for every listed table.
SELECT n.nspname, c.relname,
       has_table_privilege(current_user, c.oid, 'INSERT') AS can_insert,
       has_table_privilege(current_user, c.oid, 'UPDATE') AS can_update,
       has_table_privilege(current_user, c.oid, 'DELETE') AS can_delete,
       has_table_privilege(current_user, c.oid, 'TRUNCATE') AS can_truncate
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE (n.nspname, c.relname) IN
  (('public','clinics'), ('hp_research','clinic_hp_research'),
   ('treatment','clinic_treatment_research'), ('treatment','clinic_research_status'),
   ('provenance','comdesk_original_rows'), ('provenance','templates'),
   ('public','clinic_email_enrichment'))
ORDER BY 1, 2;

-- Expected no rows: sequence privileges or ownership when current_user is the reader.
SELECT n.nspname, c.relname, pg_get_userbyid(c.relowner) AS owner
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'S'
  AND (pg_get_userbyid(c.relowner) = current_user
       OR has_sequence_privilege(current_user, c.oid, 'USAGE')
       OR has_sequence_privilege(current_user, c.oid, 'SELECT')
       OR has_sequence_privilege(current_user, c.oid, 'UPDATE'))
ORDER BY 1, 2;

-- Compare these effective column arrays to the allowlist above.
SELECT n.nspname, c.relname,
       ARRAY(SELECT a.attname::text FROM pg_attribute a
             WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
               AND has_column_privilege(current_user, c.oid, a.attnum, 'SELECT')
             ORDER BY a.attnum) AS select_columns
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE (n.nspname, c.relname) IN
  (('public','clinics'), ('hp_research','clinic_hp_research'),
   ('treatment','clinic_treatment_research'), ('treatment','clinic_research_status'),
   ('provenance','comdesk_original_rows'), ('provenance','templates'),
   ('public','clinic_email_enrichment'))
ORDER BY 1, 2;

-- Run as clinic_cloudrun_reader: six non-email object counts should succeed. Email is conditional.
SELECT current_user, current_setting('transaction_read_only') AS transaction_read_only;
SELECT 'public.clinics' AS object_name, count(*) FROM public.clinics
UNION ALL SELECT 'hp_research.clinic_hp_research', count(*) FROM hp_research.clinic_hp_research
UNION ALL SELECT 'treatment.clinic_treatment_research', count(*) FROM treatment.clinic_treatment_research
UNION ALL SELECT 'treatment.clinic_research_status', count(*) FROM treatment.clinic_research_status
UNION ALL SELECT 'provenance.comdesk_original_rows', count(*) FROM provenance.comdesk_original_rows
UNION ALL SELECT 'provenance.templates', count(*) FROM provenance.templates;

-- Conditional: run only after email approval and its grant/policy.
SELECT count(*) AS exportable_verified_emails
FROM public.clinic_email_enrichment
WHERE status = 'VERIFIED_EMAIL' AND verified_on_official IS TRUE
  AND NULLIF(BTRIM(email), '') IS NOT NULL;

-- Expected false; do not grant these schemas for this application.
SELECT has_schema_privilege(current_user, 'extensions', 'USAGE') AS extensions_usage,
       has_schema_privilege(current_user, 'research', 'USAGE') AS research_usage,
       has_schema_privilege(current_user, 'outreach', 'USAGE') AS outreach_usage;
```

The email count query is valid only after email approval and its conditional grant/policy.
The national row counts must be compared with an independently authorized baseline to confirm
that IAP's intended nationwide population is visible; no restricted credential was available
for that live test during this review.

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
- Live catalog confirms that `clinic_cloudrun_reader` exists. It is LOGIN, INHERIT, and
  NOBYPASSRLS; it is not a member of another role. The reverse membership exists: `postgres`
  is a member of `clinic_cloudrun_reader` with ADMIN OPTION. This does not make the reader
  inherit postgres privileges. No role or membership was changed; confirm this unusual reverse
  grant is intentional.
- `public.clinics` SELECT is a direct grant by `postgres` to the reader, with no grant option;
  it is table-level (all columns). No explicit column ACLs were found.
- The reader has no sequence privileges and owns none of the seven tables. The all-schema
  privilege scan found SELECT on only `public.clinics` plus `extensions.pg_stat_statements` and
  `extensions.pg_stat_statements_info`; the latter two are granted to PUBLIC, but the reader
  currently lacks USAGE on `extensions`, so they are not queryable. Do not grant that schema.
- Catalog policies: `public.clinics` has existing SELECT policies for `clinic_email_target_reader`
  and `clinic_readonly`, plus `clinic_runtime_rw` FOR ALL; HP research has SELECT policies for
  `clinic_email_target_reader` and `clinic_readonly`, plus `clinic_runtime_rw` FOR ALL. Both
  treatment tables have `clinic_runtime_rw` FOR ALL. All these are for other roles. The reader
  policies are new and additive.
- `TEMPORARY` comes from the database ACL granted to `PUBLIC`, not a reader-specific grant. It
  permits temporary objects but not persistent writes to CRM tables; no PUBLIC/database ACL was
  changed.
- The live `clinic_email_enrichment` table had zero rows, so status distribution cannot validate
  the verified-email row predicate. Email CSV/Excel use is approved for IAP users, but DB
  privilege application remains unapproved.
- Add policies only for `clinic_cloudrun_reader`; do not drop, replace, or widen existing
  policies. All seven targets already have RLS enabled. Any other PUBLIC or inherited privileges
  must be reviewed separately before production use.
- Live check found target-table SELECT only on `public.clinics` (all 41 columns) and no table
  or column INSERT/UPDATE/DELETE/REFERENCES privileges, sequence privileges, or ownership in
  user schemas. All seven targets have RLS enabled, FORCE RLS off,
  and no policy targeting this reader. The reader is not a member of any other role; future
  membership grants to it would be inherited automatically because `INHERIT` is set.

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

Credential handling preparation (not performed): an authorized operator should retrieve the
Session Pooler host/database/user values and reader password from the approved secret source,
enter the complete DSN into the approved secret-registration interface without placing it in
shell arguments, shell history, chat, Git, SQL history, or terminal output, and grant access only
to the Cloud Run service identity through the separately approved deployment process. Validate
secret version metadata and access policy without reading the secret value. Secret Manager
registration and Cloud Run changes are prohibited in this task.

## A担当 handoff / approvals

1. C/A: retain the audit/Email KPI/HP Job/Step 6 bypass and confirm the seven-table dependency.
   Email output is approved for IAP users; keep sending, delivery history and Email KPI excluded.
   Confirm normal mode remains unchanged.
2. Confirm the deployed Cloud Run secret points to project `hwdvyhunozporfuefteb` and a
   Session Pooler URL, without exposing its value.
3. DBA/product owner must accept or separately remediate existing INHERIT, the postgres
   membership edge, table-wide clinic SELECT, and TEMP. No role change is included here.
4. Approve exact GRANT and new policy statements, then validate with the dedicated role in an
   isolated database and run read/write-denial checks on the approved target before Cloud Run.

No GRANT, RLS change, migration, data update, role membership change, password change, Cloud Run
operation, Gmail operation, or PR merge was performed for this review.
