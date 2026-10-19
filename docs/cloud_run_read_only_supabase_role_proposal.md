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

**Superseded for execution:** the initial SQL below documents the earlier full-column clinic
grant baseline. The later section “Reader column-scope decision” is the current recommendation
and contains the complete column-scoped replacement delta, including the reader-only REVOKE and
policy-conflict guard. Do not execute both SQL blocks.

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

### Cloud Run revision 00003-btp readiness (operator-reported 2026-10-19)

The operator reports that the application container started, `CLINIC_READ_ONLY_UI=true`, both
backend variables are set (their values were not inspected), `SUPABASE_RUNTIME_DB_URL` is
unset, and no Supabase connection Secret is attached. A container start is not evidence of a
successful database connection. The checked-in production routing baseline sets both
`CLINIC_DATA_BACKEND=supabase` and `CLINIC_WRITE_BACKEND=supabase`; the revision values must be
verified without printing any credential. For this deployment, both should resolve to
`supabase`, and the runtime URL should be supplied as a Secret Manager reference. With the URL
missing, the database-backed application cannot pass its required-runtime-config check; do not
fall back to SQLite or a demo dataset.

The Supabase project lookup confirms ref `hwdvyhunozporfuefteb`, name `clinic-lead-shadow`,
region `ap-northeast-1`, and healthy status. This identifies the candidate database, not the
Cloud Run target. No URL/secret value or Cloud Run service configuration was inspected. Obtain
the exact shared Session Pooler host and connection details from that project's Supabase
Dashboard Connect dialog; for shared Session mode, use port `5432` and the custom-role username
format `<role>.<project-ref>`. Do not derive the pooler hostname from the region.

### Targeted live catalog verification (SELECT-only, 2026-10-19)

The following checks were executed against project `hwdvyhunozporfuefteb`; no DDL or DML was
run:

- `clinic_cloudrun_reader` exists, LOGIN, INHERIT, NOSUPERUSER, NOBYPASSRLS,
  NOCREATEDB/NOCREATEROLE; `rolconfig` is empty (no database role default for
  `default_transaction_read_only`), and there is no `VALID UNTIL` expiry.
- Its only membership edge is the reverse edge: `postgres` is a member of
  `clinic_cloudrun_reader` with ADMIN OPTION. This does not grant postgres privileges to the
  reader; it lets the member side act as/manage that membership. The reader is not a member of
  another role. INHERIT means future memberships granted to the reader would be inherited.
- Among application tables/views, effective SELECT is currently only on `public.clinics` and
  the two `extensions.pg_stat_statements*` views. The extension views remain inaccessible
  because the reader lacks `extensions` USAGE. `public` USAGE is true; USAGE on `hp_research`,
  `treatment`, `provenance`, `research`, `outreach`, and `email_research` is false.
- All seven target tables exist, have RLS enabled and FORCE RLS disabled. No policy targets
  `clinic_cloudrun_reader`; existing CRM and service policies remain scoped to their existing
  roles. The reader currently has no SELECT on the six targets other than `public.clinics`.
- No INSERT/UPDATE/DELETE/TRUNCATE/REFERENCES/TRIGGER privileges or ownership were found for
  the reader in application schemas. CONNECT and TEMP are effective from PUBLIC; TEMP permits
  temporary objects, not persistent writes to the clinic tables. No ACL was changed.
- `public.clinic_email_enrichment` has 0 rows, so the proposed verified-email RLS behavior has
  no live rows to exercise.

The catalog tool connects with its own authorized inspection context, not as
`clinic_cloudrun_reader`. Since the reader lacks access to six required objects and no reader
credential/Session Pooler URL is available, neither RLS-visible nationwide counts nor live
read-only transaction mode has been verified. The project-level catalog confirms only that the
candidate project is reachable to the inspector.

An inspector-context baseline query returned 164,199 rows in `public.clinics` (also 164,199
unmerged and not held). This is a comparison baseline only, not evidence the reader can see
those rows. No SELECT was executed as the reader because it lacks the necessary object/schema
privileges and no reader connection is configured.

### Connection and READ ONLY verification checklist for a separately authorized operator

1. In the Cloud Run revision settings, verify the literal nonsecret values
   `CLINIC_READ_ONLY_UI=true`, `CLINIC_DATA_BACKEND=supabase`, and
   `CLINIC_WRITE_BACKEND=supabase`. Check only whether `SUPABASE_RUNTIME_DB_URL` references the
   approved secret/version; never print or export its value.
2. In the Supabase Dashboard Connect dialog for ref `hwdvyhunozporfuefteb`, select shared
   Session Pooler. Confirm the exact host, port 5432, database, TLS settings, and role-formatted
   username there. Do not guess the host or reuse `clinic_runtime` credentials.
3. After separate DB approval and applying the proposed grants/policies, run the application
   connection through a secure session without echoing the DSN. Return only booleans/counts:

   ```sql
   SELECT current_user, session_user,
          current_setting('transaction_read_only') AS transaction_read_only,
          current_setting('default_transaction_read_only') AS default_transaction_read_only;
   SELECT count(*) AS nationwide_rows,
          count(*) FILTER (WHERE merged_into IS NULL AND merge_hold IS FALSE) AS current_unmerged_rows
   FROM public.clinics;
   SELECT 'hp' AS source, count(*) FROM hp_research.clinic_hp_research
   UNION ALL SELECT 'treatment', count(*) FROM treatment.clinic_treatment_research
   UNION ALL SELECT 'treatment_status', count(*) FROM treatment.clinic_research_status
   UNION ALL SELECT 'comdesk_originals', count(*) FROM provenance.comdesk_original_rows
   UNION ALL SELECT 'templates', count(*) FROM provenance.templates
   UNION ALL SELECT 'verified_email', count(*) FROM public.clinic_email_enrichment
     WHERE status='VERIFIED_EMAIL' AND verified_on_official IS TRUE
       AND NULLIF(BTRIM(email),'') IS NOT NULL;
   ```

   Run as `clinic_cloudrun_reader`; compare clinic counts to an independently approved
   nationwide baseline. Do not emit clinic names, addresses, phone numbers, or email values in
   logs/chat. Confirm the app's query/export failure path returns a safe error and never falls
   back to SQLite, demo data, or a broader credential.
4. Verify write denial using privilege catalog functions and the transaction read-only setting.
   Do not attempt DML on production. Test actual denied DML only in an isolated disposable DB.
5. In that isolated DB, create email fixtures for valid VERIFIED_EMAIL + official=true + a
   nonblank address, and invalid status, false/null official flag, and null/blank/whitespace
   addresses. Connect as the reader and verify only the valid fixture is visible and exportable.
   Never insert fixtures into the live empty table.

For Secret Manager, an authorized operator should enter the DSN through the approved secure
secret interface, pin a numbered secret version in the Cloud Run secret reference, and grant
only the Cloud Run service identity `roles/secretmanager.secretAccessor` on that secret. Do not
put the URL in `--set-env-vars`, shell arguments, shell history, logs, source control, or chat.
Cloud Run secret configuration creates a new revision; applying it requires separate approval
and is outside this audit.

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

## Final pre-application review addendum (2026-10-19)

### Scope and code-path reconciliation

The seven-object allowlist above was rechecked against PR #119 head
`fef6b9f430b19e34dde2b41c97329ab333302ee1`. Browse/count/filter/export paths remain within
those seven objects. The READ ONLY export reads Comdesk original values and template mappings,
and reads only verified official-HP email rows. It bypasses `provenance.change_history`; the
read-only filter path avoids `research.research_results`. No query path was found for audit,
history, Email KPI, delivery, HP Job, or Step 6 tables. This is code-path evidence, not proof of
live grants or RLS visibility.

### `public.clinics` 41-column exposure

Keeping the existing table-level SELECT avoids breaking PR #119's current adapter, which uses
`effective_json` and multiple search/filter/sort fields. It also exposes all 41 columns,
including source/provenance and operational fields not needed by the two initial screens. In
particular, column-level grants alone are not a complete minimization while a wide
`effective_json` value is exposed. A column-limited direct grant would require revoking the
existing table-level ACL and proving every SQL expression still works; a safer long-term
alternative is a dedicated security-invoker projection view with an explicit column list,
backed by the same RLS, after verifying PostgreSQL/Supabase view privilege semantics. Neither
alternative is implemented here. The 41-column access is therefore a material residual risk,
not an implicitly accepted necessity. Obtain explicit data-owner approval for this exposure or
hold DB application until a reviewed projection is available.

### Existing role and additive SQL review

Live catalog findings remain: the role is LOGIN/INHERIT/NOBYPASSRLS, has the reverse membership
edge (`postgres` is a member of the reader, ADMIN OPTION), and currently has table-wide SELECT
on `public.clinics`; it has no SELECT on the other six targets, no application-table write
privileges, and no USAGE on `hp_research`, `treatment`, or `provenance`. PUBLIC supplies
CONNECT/TEMP. RLS is enabled (not FORCE) on all seven targets. Existing CRM/service policies
name other roles; proposed policies target only `clinic_cloudrun_reader`, so they are additive
and do not broaden those roles. Because permissive policies OR together for a role, the new
reader policies do not narrow access granted by any other reader-targeting policy; the live
catalog found none. `templates` is limited to template IDs referenced by visible Comdesk rows.
Do not apply until policy-name conflicts are rechecked and the broad clinic SELECT, inherited
membership posture, TEMP, and exact seven-table allowlist are approved.

The GRANT and CREATE POLICY statements above were reviewed as a delta only. They do not
REVOKE existing CRM privileges, replace policies, or alter roles. No `REVOKE` is currently
proposed because doing so would remove the existing clinic SELECT and break the current UI;
replacing it with column grants is a separate, tested change requiring a full dependency list.
Application remains prohibited until a DB change ticket explicitly approves the exact SQL.

### Email RLS isolated fixture result and persistence boundary

The predicate
`status = 'VERIFIED_EMAIL' AND verified_on_official IS TRUE AND NULLIF(BTRIM(email), '') IS NOT NULL`
was exercised in a disposable local PostgreSQL cluster with synthetic rows. Connecting under
the fixture reader returned only the valid verified/official/nonblank row; invalid status,
false/null official flag, and null/empty/whitespace email rows were invisible. The fixture
grant exposed only the four proposed columns and had no INSERT/UPDATE/DELETE privileges. This
validates predicate behavior, not production RLS or export integration. The test cluster and
fixture are local-only under `/private/tmp`; nothing was inserted into Supabase.

The live legacy `public.clinic_email_enrichment` relation had zero rows in a count-only check.
Repository persistence paths show Email Auto Research writes to the separate
`email_research.email_candidates` and `email_research.clinic_email_status` relations. Its
`existing_email_enrichment_id` foreign key is only an optional reference to the legacy row; it
does not establish synchronization or write-back. Thus the legacy export source being empty is
consistent with Auto Research results being stored in the newer schema. No row counts or
email/status values from the new schema were inspected in this review, so actual saved results
and any approved migration/backfill remain unconfirmed. Do not grant the new Email Research
tables for initial release.

### Supabase connection and secret handoff

Official Supabase connection guidance says to copy the Session Pooler endpoint from that
project's Dashboard Connect dialog. For shared Session mode, use the shown host, port 5432, and
the custom-role username form `<role>.<project-ref>`; do not synthesize a hostname from region.
The Dashboard's exact endpoint and connection values were not available to this audit, and no
reader credential was supplied. Project ref `hwdvyhunozporfuefteb` is an active candidate
project, but it has **not** been approved/confirmed as the Cloud Run revision's actual
production target. Operator-reported revision settings have no `SUPABASE_RUNTIME_DB_URL` or
attached Supabase Secret, so application DB connectivity is not configured.

For A's separately approved deployment work, the Secret Manager payload should be the complete
PostgreSQL DSN using the exact Dashboard-provided Session Pooler host, port, database, TLS
settings, role-formatted username, and dedicated reader password. Store it by entering/piping
through the approved secret UI without command-line arguments, shell history, logs, Git, or
chat. Configure Cloud Run `SUPABASE_RUNTIME_DB_URL` as a reference to a numbered Secret Manager
version and grant `roles/secretmanager.secretAccessor` on that secret only to the Cloud Run
service identity. Keep `CLINIC_READ_ONLY_UI=true`, `CLINIC_DATA_BACKEND=supabase`, and
`CLINIC_WRITE_BACKEND=supabase`; verify only these nonsecret values. No secret, IAM, or Cloud
Run configuration was changed.

After separate DB approval and privilege application, the reader connection check should return
`current_user = clinic_cloudrun_reader`, the expected `session_user`, and both transaction
read-only settings true after the application's connection initialization. Run `SELECT 1`,
allowlisted counts, representative clinic filters, and a CSV/Excel export; compare only counts
to an independently approved national baseline. Verify denied writes using catalog privilege
functions and actual denied write attempts only in an isolated database. The application must
fail closed on missing permissions and must not fall back to SQLite, demo data, or a broader
credential. A reader credential and exact pooler endpoint are still required before this can
be tested end-to-end.

### Boundary note and decision

During this review, two aggregate row-count SELECTs were inadvertently issued against the
production candidate (`clinic_email_enrichment` and `clinics`) despite the instruction to limit
production queries to catalog inspection. They returned counts only and no row values; no
write or schema change occurred. This was outside the requested query boundary and is recorded
here explicitly. All subsequent live checks were catalog-only. Do not repeat aggregate/data
queries until separately authorized.

**Decision remains NO-GO for production DB application.** Blockers: the Cloud Run target
project/ref and exact Session Pooler endpoint are unconfirmed; no reader credential or live
reader session exists; six required SELECT grants and three schema USAGE grants plus reader RLS
policies are absent; 41-column clinic access needs data-owner acceptance or a reviewed
projection; the unusual reverse membership/INHERIT and PUBLIC TEMP require owner acceptance;
and the email export source is empty while Email Auto Research persistence remains a separate,
unverified data source. The email predicate passed only an isolated fixture test. The seven-table
SQL remains a review proposal and may be applied only after explicit change approval and all
blockers above are resolved.

## Reader column-scope decision (2026-10-19)

The 41-column table-level grant is not needed for the two initial READ ONLY screens. PR #119
head `fef6b9f430b19e34dde2b41c97329ab333302ee1` was checked at its actual READ ONLY query
methods, filter compiler, row projection and standard export. The current direct-query
allowlist is:

```text
id, uuid, merged_into, merge_hold, effective_json, tel_match_key,
first_seen_at, last_seen_at, source_as_of_date, is_new, departments_json,
hp_status, hp_url, hp_rank, maps_website_url, exclude_reason, clinic_name,
active, designation_date, recent_until, age_probability, prefecture,
medical_type, hot_status, treatments_json, signal_count, signals_json,
owner_equal, maps_presence_status, phone_norm, address, registration_reason
```

The selected columns above cover the adapter's row fetch and record construction. The SQL
filter/order paths additionally need `merge_hold`, `exclude_reason`, `effective_json`,
`clinic_name`, `active`, `hp_status`, `hp_url`, `designation_date`, `recent_until`,
`age_probability`, `hp_rank`, `prefecture`, `medical_type`, `hot_status`,
`departments_json`, `treatments_json`, `signals_json`, `signal_count`, `owner_equal`,
`registration_reason`, `maps_presence_status`, `maps_website_url`, `phone_norm`, `uuid`,
`first_seen_at`, and `id`. The read-only sales screen also derives records from `effective_json`
for display and fixed export rows; the read-only workflow's excluded-facility filter uses it.
HP rank/category reads come from the separately granted research tables. Comdesk original rows
and template mapping are read from the two provenance tables already in the seven-object list.
The read-only listing bypasses Email Research status and detail reads. The normal-mode adapter
and Clinic Master/CRM role grants are outside this change.

**Recommendation:** replace the reader's existing all-column SELECT on `public.clinics` with
the above reader-only column grant. It preserves current UI SQL while preventing access to
other standalone columns. This is less exposure than all 41 columns and the revoke is addressed
only to `clinic_cloudrun_reader`, leaving Clinic Master and CRM grants/policies untouched.
However, `effective_json` is a wide JSON document; granting that column exposes fields embedded
inside it even though the role cannot select other standalone columns. Treat its access as an
explicit data-owner approval gate. A later dedicated projection that returns only the keys
needed for display/export would reduce this residual risk further, but requires coordinated UI
query/export adaptation and role-scoped tests; it is not safe to claim that a column grant on
the current `effective_json` value is complete data minimization.

### Final proposed SQL and order (not executed)

Before the transaction, verify the target project/database and role manually, and run the
catalog-only preflight queries above. The following assumes the audited state remains true:
`public.clinics` table-level SELECT was directly granted to this role; no PUBLIC or inherited
SELECT path exists; all seven targets already have RLS enabled; and none of the proposed policy
names exists. If any assumption differs, stop and revise the change. Do not modify other roles,
CRM grants, existing policies, role attributes/memberships or RLS enablement.

Order is schema USAGE, narrow grants, reader policy on Comdesk originals, then dependent template
policy. Clinics gets a reader-only column grant after revoking its existing reader table grant.
RLS policies are then added for the reader. Execute in one reviewed transaction so any policy
name collision or statement error rolls the whole delta back.

```sql
BEGIN;

-- Explicit conflict guard: fail closed; never replace an existing policy.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_policies
    WHERE policyname = 'clinic_cloudrun_reader_select'
      AND ( (schemaname, tablename) IN (
        ('public','clinics'), ('hp_research','clinic_hp_research'),
        ('treatment','clinic_treatment_research'), ('treatment','clinic_research_status'),
        ('provenance','comdesk_original_rows'), ('provenance','templates'),
        ('public','clinic_email_enrichment')
      ))
  ) THEN
    RAISE EXCEPTION 'clinic_cloudrun_reader_select policy name conflict; stop for review';
  END IF;
END $$;

GRANT USAGE ON SCHEMA hp_research, treatment, provenance TO clinic_cloudrun_reader;

-- Remove only this role's existing table-level grant; leave all other grantees untouched.
REVOKE SELECT ON TABLE public.clinics FROM clinic_cloudrun_reader;
GRANT SELECT (
  id, uuid, merged_into, merge_hold, effective_json, tel_match_key,
  first_seen_at, last_seen_at, source_as_of_date, is_new, departments_json,
  hp_status, hp_url, hp_rank, maps_website_url, exclude_reason, clinic_name,
  active, designation_date, recent_until, age_probability, prefecture,
  medical_type, hot_status, treatments_json, signal_count, signals_json,
  owner_equal, maps_presence_status, phone_norm, address, registration_reason
) ON TABLE public.clinics TO clinic_cloudrun_reader;

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
GRANT SELECT (clinic_id, email, status, verified_on_official)
  ON TABLE public.clinic_email_enrichment TO clinic_cloudrun_reader;

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
CREATE POLICY clinic_cloudrun_reader_select ON public.clinic_email_enrichment
  FOR SELECT TO clinic_cloudrun_reader
  USING (status = 'VERIFIED_EMAIL'
         AND verified_on_official IS TRUE
         AND NULLIF(BTRIM(email), '') IS NOT NULL);

COMMIT;
```

The `REVOKE` is the only revocation in the proposed delta and applies solely to the reader's
`public.clinics` table-level SELECT. Post-apply, verify the effective column array equals the
allowlist, all seven RLS policies target only this role, all write privileges remain false, and
all six other target SELECT grants match the column allowlists. Also verify `public.clinics`
standalone columns outside the allowlist are denied; review the `effective_json` data risk
separately. Then test as the dedicated role through the exact Session Pooler: expected
`current_user`, read-only transaction settings, `SELECT 1`, national count and approved
filters/export. Do not include personal field values in diagnostic output. Denied DML is tested
only in an isolated database.

### Manual approval and change gate

- Confirm the Cloud Run target project ref/database. `config/production_runtime.env` establishes
  Supabase as the backend but does not name a project. Existing operations documents identify
  `hwdvyhunozporfuefteb` as a candidate; this does not prove the deployed service targets it.
- Resolve the role-name mismatch between this audit's existing `clinic_cloudrun_reader` and the
  separate Cloud Run design's `clinic_web_readonly`. Never silently substitute or create either.
- Approve the `effective_json` exposure or defer application until the projection/UI adjustment
  is ready. Approve the exact SQL and verify no policy-name conflict at application time.
- In Supabase Dashboard > Connect, select the approved project's Session Pooler mode and copy
  the exact host, port, database, TLS and username instructions. For shared Session Pooler the
  documented port is 5432 and custom-role username form is `<role>.<project-ref>`; do not infer
  the host from a region. Provision the dedicated password only through the approved secret
  procedure, never in SQL or shell history.
- A separately approved operator configures Cloud Run's `SUPABASE_RUNTIME_DB_URL` as a
  numbered Secret Manager version reference, grants secret access only to the Cloud Run service
  identity, and verifies the nonsecret backend flags. No such operation is authorized here.

**Application decision: NO-GO** until target/ref and role are confirmed, the data owner accepts
or removes the `effective_json` residual exposure, the SQL and existing role posture are
approved, and a real Session Pooler reader connection is tested. The proposed column-scoped
delta does not change CRM roles or policies; the projection alternative is a future coordinated
UI/DB change, not part of this SQL.

## A handoff and manual application preparation (2026-10-19)

### Candidate database and role selection

The user supplied Dashboard Connect details for project ref `hwdvyhunozporfuefteb`, project
`clinic-lead-shadow`: Session Pooler host `aws-0-ap-northeast-1.pooler.supabase.com`, port
`5432`, database `postgres`. This matches the project ref/name/region used by the prior live
catalog audit and existing Clinic Master operations/migration records. `config/production_runtime.env`
only selects the Supabase backend and does not encode a project ref. Therefore this is the same
database candidate as the existing Clinic Master, but the Cloud Run revision's actual target
and its production approval still require the service owner to confirm against the deployment
record; no Cloud Run inspection was performed here.

Use existing `clinic_cloudrun_reader` as the first-choice role for this proposal: catalog
review found it exists and is LOGIN/INHERIT/NOBYPASSRLS. The separate `clinic_web_readonly`
name in `docs/cloud_run_job_control_db_role_design.md` is a design assumption for the web role,
not the role observed in this DB audit. Do not create or silently substitute it. The role's
password state is unverified; LOGIN being true does not prove a password is provisioned. The
previous operator report said the Cloud Run revision had no `SUPABASE_RUNTIME_DB_URL` and no
attached Supabase Secret. Secret Manager state and local reader credentials have not been
rechecked in this pass; do not infer they have since been provisioned.

The Dashboard's displayed administrator username `postgres.hwdvyhunozporfuefteb` is for
human-administered maintenance only and must never be placed in Cloud Run. For the reader,
the expected shared Session Pooler user form is
`clinic_cloudrun_reader.hwdvyhunozporfuefteb`. The exact host above was supplied from
Dashboard; do not derive or modify it. Use TLS as specified by the Dashboard connection dialog
(the operator must confirm the displayed TLS requirement).

### `effective_json` exposure assessment

`effective_json` stores the full effective clinic/source dictionary used to project a clinic,
not only the fields rendered in the READ ONLY table. The application constructs it from
normalized source data and can include clinic name, telephone, address, postal/prefecture and
facility classifications, departments, provider/manager and owner fields, registration and
business-hours details, HP/Maps properties, treatment categories, marketing-signal details, and
source-specific keys. Exact keys can vary by source and record. The dedicated verified-email
address is read from the separate enrichment table, not inferred to be part of this JSON.

IAP limits which internal users reach the app, but it does not constrain SQL executed with the
service credential and does not prevent a compromised app or an overly broad export from reading
any granted JSON value. Thus column-level SELECT is materially narrower than all 41 columns,
but granting `effective_json` retains meaningful data exposure. For the current UI/CSV contract,
it is included in the proposed grant and needs explicit data-owner acceptance. If that is not
accepted, hold DB application and first implement a reader projection plus matching adapter and
export changes; do not grant all columns as a workaround.

### Manual connection and credential check

No reader password was displayed, tested, or changed. A human operator with the approved
credential source should check only whether the credential/secret exists and is available to
the intended service identity; never print its value. If no reader credential exists, its
provisioning is a separately approved DB-admin action, outside this proposal. Do not use the
Dashboard administrator credential for the application.

From a controlled terminal, a human DBA may connect with an interactive password prompt. The
connection string contains no password, and `-W` prompts without placing one in shell history:

```sh
psql "host=aws-0-ap-northeast-1.pooler.supabase.com port=5432 dbname=postgres user=postgres.hwdvyhunozporfuefteb sslmode=require" -W
```

Use that administrator session only for the SELECT-only pre-apply catalog checks in this
document. After a separately approved apply, test the dedicated reader through its own
interactive prompt (never as `postgres`):

```sh
psql "host=aws-0-ap-northeast-1.pooler.supabase.com port=5432 dbname=postgres user=clinic_cloudrun_reader.hwdvyhunozporfuefteb sslmode=require" -W
```

Do not use `PGPASSWORD`, put a DSN containing a password on a command line, enable shell
tracing, or copy credential values into logs/chat. If Dashboard specifies different TLS
parameters, follow that exact connection dialog rather than the illustrative `sslmode=require`.

### Post-apply reader verification SQL (prepared only)

Run only after separate approval and application. These checks emit identity, booleans, policy
metadata and counts, not clinic/email values:

```sql
SELECT current_user, session_user, current_database(),
       current_setting('transaction_read_only') AS transaction_read_only,
       current_setting('default_transaction_read_only') AS default_transaction_read_only;

SELECT n.nspname, c.relname,
       array_agg(a.attname ORDER BY a.attnum)
         FILTER (WHERE a.attnum > 0 AND NOT a.attisdropped
                 AND has_column_privilege(current_user, c.oid, a.attnum, 'SELECT'))
         AS select_columns,
       has_table_privilege(current_user, c.oid, 'INSERT') AS can_insert,
       has_table_privilege(current_user, c.oid, 'UPDATE') AS can_update,
       has_table_privilege(current_user, c.oid, 'DELETE') AS can_delete,
       has_table_privilege(current_user, c.oid, 'TRUNCATE') AS can_truncate
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_attribute a ON a.attrelid = c.oid
WHERE (n.nspname, c.relname) IN
  (('public','clinics'), ('hp_research','clinic_hp_research'),
   ('treatment','clinic_treatment_research'), ('treatment','clinic_research_status'),
   ('provenance','comdesk_original_rows'), ('provenance','templates'),
   ('public','clinic_email_enrichment'))
GROUP BY n.nspname, c.relname, c.oid ORDER BY 1, 2;

SELECT schemaname, tablename, policyname, roles, cmd, qual
FROM pg_policies
WHERE 'clinic_cloudrun_reader' = ANY(roles)
  AND (schemaname, tablename) IN
    (('public','clinics'), ('hp_research','clinic_hp_research'),
     ('treatment','clinic_treatment_research'), ('treatment','clinic_research_status'),
     ('provenance','comdesk_original_rows'), ('provenance','templates'),
     ('public','clinic_email_enrichment'))
ORDER BY 1, 2, 3;

SELECT has_schema_privilege(current_user, 'hp_research', 'USAGE') AS hp_usage,
       has_schema_privilege(current_user, 'treatment', 'USAGE') AS treatment_usage,
       has_schema_privilege(current_user, 'provenance', 'USAGE') AS provenance_usage,
       has_schema_privilege(current_user, 'research', 'USAGE') AS research_usage,
       has_schema_privilege(current_user, 'outreach', 'USAGE') AS outreach_usage;

SELECT 1;
-- Only after the privacy/change approval: counts may be compared to an approved national
-- baseline. Never return clinic names, contact details or email addresses in diagnostics.
SELECT count(*) AS clinic_rows FROM public.clinics WHERE merged_into IS NULL;
SELECT count(*) AS eligible_verified_email_rows
FROM public.clinic_email_enrichment
WHERE status='VERIFIED_EMAIL' AND verified_on_official IS TRUE
  AND NULLIF(BTRIM(email), '') IS NOT NULL;
```

Expected: reader identity matches the approved role; both read-only settings are `on`; exact
column allowlists and seven policies match the proposal; all writes are false; the three needed
schema USAGE values are true while `research`/`outreach` remain false; SELECT and app filters/
export succeed. Check denied DML only in an isolated DB, not on the production candidate.

### Rollback proposal (review only)

If rollback is approved, first stop routing application traffic to this credential and drain
its sessions. Then, as the authorized DBA, run the following in one transaction. It removes only
the newly named reader policies/grants and restores the exact pre-change table-level clinic
SELECT grant. It does not touch CRM roles/policies or role attributes:

```sql
BEGIN;
DROP POLICY clinic_cloudrun_reader_select ON public.clinics;
DROP POLICY clinic_cloudrun_reader_select ON hp_research.clinic_hp_research;
DROP POLICY clinic_cloudrun_reader_select ON treatment.clinic_treatment_research;
DROP POLICY clinic_cloudrun_reader_select ON treatment.clinic_research_status;
DROP POLICY clinic_cloudrun_reader_select ON provenance.comdesk_original_rows;
DROP POLICY clinic_cloudrun_reader_select ON provenance.templates;
DROP POLICY clinic_cloudrun_reader_select ON public.clinic_email_enrichment;

REVOKE SELECT (id, uuid, merged_into, merge_hold, effective_json, tel_match_key,
  first_seen_at, last_seen_at, source_as_of_date, is_new, departments_json,
  hp_status, hp_url, hp_rank, maps_website_url, exclude_reason, clinic_name,
  active, designation_date, recent_until, age_probability, prefecture,
  medical_type, hot_status, treatments_json, signal_count, signals_json,
  owner_equal, maps_presence_status, phone_norm, address, registration_reason)
  ON TABLE public.clinics FROM clinic_cloudrun_reader;
REVOKE SELECT (clinic_id, machine_hp_rank, fetch_status, hp_url, final_url, treatment_categories)
  ON TABLE hp_research.clinic_hp_research FROM clinic_cloudrun_reader;
REVOKE SELECT (clinic_id, research_status, treatment_category_name)
  ON TABLE treatment.clinic_treatment_research FROM clinic_cloudrun_reader;
REVOKE SELECT (clinic_id, research_status)
  ON TABLE treatment.clinic_research_status FROM clinic_cloudrun_reader;
REVOKE SELECT (id, clinic_id, template_id, row_json, uuid)
  ON TABLE provenance.comdesk_original_rows FROM clinic_cloudrun_reader;
REVOKE SELECT (id, headers_json, mapping_json)
  ON TABLE provenance.templates FROM clinic_cloudrun_reader;
REVOKE SELECT (clinic_id, email, status, verified_on_official)
  ON TABLE public.clinic_email_enrichment FROM clinic_cloudrun_reader;
REVOKE USAGE ON SCHEMA hp_research, treatment, provenance FROM clinic_cloudrun_reader;
GRANT SELECT ON TABLE public.clinics TO clinic_cloudrun_reader;
COMMIT;
```

This rollback SQL is valid only if the pre-change condition remains true (the reader had its
table-level clinic SELECT and no separate clinic column grants/use created meanwhile). Check
post-rollback effective privileges; never run blindly or during an active connection.

**Latest handoff status: NO-GO for DB application.** The pooler endpoint is now user-confirmed,
and it matches the previously audited Clinic Master project. Remaining gates are explicit
Cloud Run target/project and role approval, `effective_json` risk acceptance or a projection
refactor, confirmation/provisioning of the reader credential, approval of the exact seven-table
delta, and a successful post-approval reader connection/verification. This addendum supersedes
earlier statements that the pooler host was unknown; it does not claim that authentication,
Cloud Run wiring, or DB permissions are ready.

## Main integration recheck and final release gate (2026-10-19)

The user-supplied current main SHA `c5829aa38a6d2fecc3023ffaa38181ab7e41f02f` was fetched and
confirmed. It contains PR #119 (`0221ef3`) and PR #120 (`e67a41b`) merged. The READ ONLY
`count_read_only_ui`, `query_read_only_ui`, `export_read_only` paths, audit/email/job bypasses,
Gmail draft guards, and existing export tests are present on this main. The audited dependency
boundary remains seven tables; no additional DB object is required by the #119/#120 integration.
No application tests were rerun for this recheck. Prior integration results remain the evidence
for those code paths; they are not a live-reader DB test.

The role/RLS audit in this document is the latest available live catalog evidence and has not
been repeated in this turn. It found `clinic_cloudrun_reader` LOGIN/INHERIT/NOBYPASSRLS, the
reverse `postgres` membership edge with ADMIN OPTION, PUBLIC CONNECT/TEMP, all seven target
tables RLS-enabled, no reader policy on them, existing table-wide SELECT only on
`public.clinics`, and no persistent write privileges on the targets. Before application, the
DBA must rerun the listed catalog-only preflight against the approved target and stop if any of
those assumptions or policy-name checks changed. The existing CRM policies/grants remain out
of scope.

The column-scoped clinic proposal is the intended delta. It grants no access to standalone
columns outside the UI/SQL allowlist, but it still grants `effective_json`. That JSON contains
the full effective source dictionary, including identifying/contact and operational details
(clinic/owner/manager names, address/phone, classification, registration/business-hours,
HP/Maps, treatment/signal data and source-specific fields, depending on source). IAP restricts
application entry but is not a database row/column boundary. Until a data owner explicitly
approves the entire JSON payload for IAP-authorized users, the safe launch decision is NO-GO.
The shortest secure choices are either that explicit approval with the current narrowed SQL, or
a coordinated UI/projection change that removes the wide JSON read before applying grants.

### Final operator checklist

1. Approve project ref `hwdvyhunozporfuefteb` as Cloud Run's target (it matches prior Clinic
   Master catalog/operations records; checked-in runtime config itself does not name the ref).
2. Approve the existing role `clinic_cloudrun_reader`. Do not use or create
   `clinic_web_readonly` unless the owner explicitly changes the design. Confirm the role's
   password is provisioned through an approved process; this audit did not check a password hash
   or authenticate as the reader.
3. Approve/reject the full `effective_json` content for IAP users. Rejecting it means no DB
   application until the UI reads from a minimized projection.
4. DBA reruns the documented pre-apply catalog checks and policy-name guard, reviews the exact
   GRANT/REVOKE/RLS transaction, and applies it only under a separate change authorization.
5. A human enters the DSN in Secret Manager through its secure interface. Secret payload uses
   the Dashboard host/port/database, Reader username, Dashboard TLS instructions, and the
   separately provisioned Reader password. Cloud Run variable is `SUPABASE_RUNTIME_DB_URL`
   referencing a numbered Secret version. The administrator user shown by Dashboard is never
   used by Cloud Run. Secret and IAM actions are separate approvals.
6. Verify first with the dedicated role through the shared Session Pooler, then run the SQL and
   app acceptance checks above. Compare nationwide counts only against an approved baseline;
   never print personal clinic/email values. Validate write denial by effective privileges and
   transaction-read-only settings in production; attempt DML only in an isolated DB.

### Rollback choice

The rollback SQL above restores the pre-change ACL exactly, including the pre-existing
table-wide 41-column clinic SELECT. It is an availability rollback and therefore reopens the
same `effective_json`/column exposure that existed before this grant change. The DBA and data
owner must choose that explicitly. If the incident is a data-exposure concern, do not restore
the table-wide grant: stop Cloud Run traffic/secret use through separately approved operator
actions, drop only the seven new reader policies, revoke all new reader column/schema grants,
and leave the reader with no `public.clinics` SELECT. Re-enable the pre-change broad ACL only
after the security owner approves that restoration. In either case, do not touch CRM grants or
policies.

**Final decision:** SQL and operator procedures are prepared; production application is
**NO-GO** until the target project, role/password readiness, and `effective_json` data-use scope
are explicitly approved, the pre-apply catalog state is reconfirmed, and a post-apply Reader
Session Pooler test passes. PR #118 remains Draft and unmerged.
