# Mac Step1–7 dedicated connections — approval package

Status: code and SQL review only. No production role, GRANT, policy or DML was applied.

## Step6 production findings (2026-10-27, READ ONLY)

`SupabaseRuntimeStore.latest_completed_hp_job()` succeeds for job
`6a825bdfc91e43959649a9c95dabc49e`. The next query in
`hp_job_export_summary()` joins `public.clinics c` in both the current-job
summary and cumulative waiting-list SELECT. `clinic_cloudrun_reader` receives
SQLSTATE `42501`, `permission denied for table clinics`, at the first SELECT.
`SupabaseProvenanceRepository.comdesk_linkage_state()` starts with
`SELECT clinic_id FROM provenance.source_records WHERE source='コムデスク'`;
that SELECT receives SQLSTATE `42501`, `permission denied for table source_records`.
After those gates, Step6 also reads HP results, Human Review, provenance,
verified email, canonical sales identity and the original 30-column export
values. Adding only two table grants would produce further failures.

The existing `clinic_email_target_reader` can read Comdesk source records and
Clinic Master, but cannot read `research` schema, `provenance.templates`, HP
Human Review, verified-email enrichment or sales identity. Reusing it would
couple Step6 to Email Research and still require many additional grants.

## Reader approval

Review [step6_reader_apply.sql](step6_reader_apply.sql) and
[step6_reader_rollback.sql](step6_reader_rollback.sql). The apply script creates
`clinic_comdesk_export_reader`, grants SELECT on only the columns used by the
existing Step6 export and Step7 UUID-diff repository, and adds SELECT-only RLS
policies. It does not change `clinic_cloudrun_reader`, `clinic_runtime`,
existing policies, View definitions or View ownership. Existing `public.clinic_cloudrun_readonly`
keeps its owner `clinic_cloudrun_projection_owner`; the new role receives only
`id`, `hp_url`, `maps_website_url` from that View for the dashboard metric.

The `public.clinics.effective_json` and `base_json`, `research_results.result_json`,
and `provenance.comdesk_original_rows.row_json` columns contain multiple source
fields. They are required by the current 30-column exporter and Step7 preview;
they are the main data exposure to review before approving the new role. RLS
limits the new role's `source_records` and `match_reviews` rows to Comdesk,
`clinic_email_enrichment` to verified official Email, and Job items to HP.
No direct Email-candidate relation is granted. No role receives DML from this SQL.

The SQL intentionally contains no password. After approval and application,
the operator sets a new credential through the approved secret channel and
creates `~/.config/clinic-lead/comdesk-reader.env` with a single line named
`SUPABASE_COMDESK_EXPORT_READER_DB_URL`; set mode `600`. The URL must use the
new role and the same Supabase project as the existing Clinic Runtime. The
application verifies the actual PostgreSQL role, absence of DML/DDL privileges,
required column grants and visible Clinic count before Step6/7 reads. It never
uses the HP Writer as a Reader.

## Import and sync Writers

Mac code routes each apply operation through a dedicated role, checked against
the active Supabase project and `current_user` before DML. Place only the
following named values in `~/.config/clinic-lead/feature-writers.env` with
mode `600`, after each role has been reviewed and provisioned:

| Step | Environment variable | Required database role | Existing Repository method |
| --- | --- | --- | --- |
| 1 | `SUPABASE_COMDESK_IMPORT_WRITER_DB_URL` | `clinic_comdesk_import_writer` | `clinics.import_comdesk` |
| 2 | `SUPABASE_KOUSEIKYOKU_IMPORT_WRITER_DB_URL` | `clinic_kouseikyoku_import_writer` | `clinics.import_master` |
| 4 | `SUPABASE_MAPS_IMPORT_WRITER_DB_URL` | `clinic_maps_import_writer` | `provenance.import_maps_results` |
| 7 | `SUPABASE_COMDESK_SYNC_WRITER_DB_URL` | `clinic_comdesk_sync_writer` | `clinics.import_comdesk`, `clinics.apply_comdesk_sync` |

The code opens none of these Writer connections on page load. Steps 1 and 2
show local file previews without a Writer. Step 4 matching preview requires
its dedicated Writer's SELECT rights and rolls the read transaction back.
Step 7 preview uses only `clinic_comdesk_export_reader`; apply rechecks the
plan before opening `clinic_comdesk_sync_writer`. Each apply action still
requires its original button and a confirmation. These roles have **not** been
created and no DML grant proposal for them has been approved. Their DML policy
must be reviewed separately against each Repository's mutation tables before
setting any of the four values. Do not copy `runtime-writer.env` into this file.

## Dashboard and HP-target denominator

The dashboard's `not_researched` SQL starts with all Clinics having `hp_url`
or `maps_website_url`, then counts rows without a `clinic_hp_research` row.
The HP Job selector additionally requires `active=true`, unmerged/unheld,
Maps official website URL and no active Job reservation or past SUCCESS.
The same-day READ ONLY count was:

| Sequential condition | Remaining |
| --- | ---: |
| URL present, no HP ledger row | 13,123 |
| Medical or dental | 13,116 |
| Also active | 0 |
| Also unmerged and not held | 0 |
| Also Maps official URL | 0 |
| Also not reserved by active/paused/budget HP Job | 0 |

The 13,123 source rows were all inactive, unmerged, not held, had Maps official
URL, and had no active/paused/budget Job claim. `clinic_cloudrun_reader` can
see all 13,123 through `public.clinic_cloudrun_readonly`; RLS did not cause
the difference. Medical-only accounts for 8,251; seven rows are outside
medical/dental. The UI currently has 0 eligible UNRESEARCHED, 3,405 ERROR and
3,080 REVIEW candidates (6,485 selectable in total). SUCCESS is browse-only.
