# PR #121/#122 READ ONLY projection and PR #118 DB handoff

This is a proposed DB contract. It has only been applied to a disposable loopback PostgreSQL
fixture. Production DB, Secret Manager, IAM/IAP and Cloud Run remain unchanged.

## Reader relation contract

`clinic_cloudrun_reader` reads seven relations: `public.clinic_cloudrun_readonly`,
`provenance.clinic_cloudrun_comdesk_rows`, `provenance.templates`,
`hp_research.clinic_hp_research`, `treatment.clinic_treatment_research`,
`treatment.clinic_research_status`, and `public.clinic_email_enrichment`.
It has no direct SELECT on `public.clinics` or `provenance.comdesk_original_rows` and no
INSERT/UPDATE/DELETE/TRUNCATE on those seven relations. Existing Mac/runtime credentials and
normal-mode SQL paths retain their current grants and behavior.

`public.clinic_cloudrun_readonly` contains the named clinic columns in `_READ_ONLY_COLUMNS`,
`effective_projection jsonb`, `projection_marker text`, `projection_valid boolean`, and
`is_hospital_or_center boolean`. The JSON projection contains only
`hp_production_companies`, `epark_url`, and `epark_contract`. It excludes raw facility type and evidence JSON,
inferred age, names, schedules, and `treatment_status`. The marker depends only on the already
visible Clinic ID. The hospital boolean uses the same literal `LIKE` predicate as normal mode.

`provenance.clinic_cloudrun_comdesk_rows` returns `id bigint`, `clinic_id bigint`,
`template_id bigint`, `uuid text`, `projection_valid boolean`, and a position-preserving
`row_json jsonb` array. Values are retained only for these existing standard Comdesk headers:
`UUID`, `種別`, `名前`, `カナ`, `郵便番号`, `都道府県`, `住所１`, `住所２`, `住所カナ`,
`Tel1`–`Tel4`, `FAX`, `URL`, `旧社名`, `リードソース`, `開業日`.
Unknown headers and doctor, schedule, notes, history, and article fields become empty strings.
The source row and template header arrays must have equal lengths; otherwise READ ONLY export
fails closed. CSV/XLSX still has the established 30 headers, original name/phone priority,
HP rank and qualifying official verified email. Pending/redacted values are intentionally blank.
The data owner must approve this remaining contact-field list before production use.

## Exact PR #118 proposal delta

- Replace the Reader grant on `provenance.comdesk_original_rows(id,clinic_id,template_id,row_json,uuid)`
  with `GRANT SELECT ON provenance.clinic_cloudrun_comdesk_rows TO clinic_cloudrun_reader`.
  Do not regrant direct `row_json` after applying PR #121's migration.
- Preserve the existing Reader grants/policies for templates, HP research, both treatment
  relations, and verified email. Keep the verified-email RLS condition
  `status='VERIFIED_EMAIL' AND verified_on_official IS TRUE AND NULLIF(BTRIM(email),'') IS NOT NULL`.
- Use a new `NOLOGIN NOINHERIT NOBYPASSRLS NOSUPERUSER` projection owner with no memberships
  and no ownership of either source table. Grant it only the source columns needed by the two
  views; the Reader receives neither raw source table.
- The helper's `USING (true)` policy intentionally permits the approved national row scope.
  The migration aborts if `public.clinics` or the Comdesk sources have a PUBLIC policy or a helper/view/function name
  collision. B must inspect the live policy composition and confirm that no effective scope
  widens for other roles before applying. Existing restrictive policies continue to apply.
- The function is create-only; its default PUBLIC EXECUTE is revoked in the same transaction.
  Verify the function ACL, view ownership, helper `CREATE=false` on `public` and `provenance`,
  and Reader direct-source/DML denial after application.

## Application order and release gates

1. B confirms the target Supabase project, exact Session Pooler endpoint, role attributes,
   schema/object collisions, current grants, RLS policies and nationwide baseline counts.
2. A data owner approves the named clinic and remaining Comdesk contact-field lists. If a field
   is denied, remove it from the projection contract and rerun export parity tests.
3. In a separately approved DBA-controlled transaction, apply the revised PR #118 grants for
   the other relations and PR #121's two-view migration. Never apply the old direct Comdesk
   `row_json` grant afterward.
4. B checks effective grants, RLS visibility, function PUBLIC EXECUTE denial, Reader DML denial,
   national count/filter/export parity and query latency using the approved target. Compare
   on a representative isolated copy before claiming production-scale performance.
5. A confirms a dedicated Reader Session Pooler credential through the approved secret channel.
   The write-capable `clinic_runtime` credential is not a substitute.
6. After code review and separate merge/build approval, A creates a revision of the existing
   Cloud Run service with no traffic. Verify IAP allowed/denied users, the two READ ONLY screens,
   national filters and CSV/XLSX before any traffic change. Keep the prior revision available
   for rollback; traffic change and rollback require their own approval.

The repository Actions workflow only runs pytest. `Dockerfile.cloudrun` does not copy
`supabase/migrations`; merging or building this code does not itself apply the migration.
An external Cloud Build trigger configuration has not been inspected here.
