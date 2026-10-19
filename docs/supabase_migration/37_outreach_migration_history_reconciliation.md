# Outreach migration history reconciliation — READ ONLY findings

Date: 2026-10-19. Project: `clinic-lead-shadow`. This is a read-only comparison of repository migration filenames, Supabase migration history, and the supplied shadow catalog snapshot. No database writes, migration repairs, role creation, or migration application were performed.

## Finding: repository and remote history are not in sync

Supabase `list_migrations` reports these identity-related versions:

| Supabase history version | Recorded name | Repository counterpart / note |
|---|---|---|
| `20261019082957` | `clinic_sales_entity_identity_20261019064657` | SQL corresponds by name to repository `20261019064657_clinic_sales_entity_identity.sql`; versions differ. |
| `20261019084031` | `harden_clinic_sales_entity_identity_acl` | Repository file is `20261019120000_harden_clinic_sales_entity_identity_acl.sql`; versions differ. |
| `20261019090131` | `clinic_runtime_sales_entity_access` | No file with this version is in `supabase/migrations/`; locate and review its source before treating history as fully reconstructable. |
| `20261019090312`, `20261019090351`, `20261019090531`, `20261019090552` | temporary runtime-role validation grants and their removals | These are recorded history entries; validate their final catalog effects before any reconciliation. |

The remote history also contains the earlier core, Stage 4D, and email schema versions. It does **not** contain repository version `20261016172806` (`outreach_email_delivery_records`) or the #112 versions `20261019140000`–`20261019151000`. The supplied shadow catalog snapshot says `outreach.email_delivery_records`, `outreach.clinic_draft_guard`, and `outreach.sales_cases` are absent, `clinic_outreach_runtime` is absent, while `outreach.email_contact_events` and `outreach.clinic_email_summary` exist. These facts are consistent with a partially provisioned schema and independently recorded migration history; they do not prove that any similarly named historical SQL can safely be replayed.

The #115 proposal is now `20261019160000_outreach_full_sheets_gmail_integration.sql`, after #112's `20261019140000`, `19140100`, `19140200`, `19150000`, and `19151000` files. Repository-wide filename inspection found no collision at `20261019160000`. This does not repair or normalize existing remote history.

## Required production preflight and reconciliation

1. Freeze all migration deployment attempts. Use a single designated migration operator and an approved change window.
2. With the intended project explicitly selected, capture `supabase migration list` output and a read-only catalog inventory (roles, schemas, relations, constraints, grants, RLS policies, and view definitions). Save timestamped copies with the deployment record. Do not rely on project display name alone.
3. Compare every local migration version to Supabase's `supabase_migrations.schema_migrations` history. Compare the SQL and catalog effects for the two identity migrations whose remote versions differ from repository versions; resolve the untracked `clinic_runtime_sales_entity_access` source; verify that the temporary grants were removed.
4. For each mismatch, classify it as (a) migration ran and history is recorded under another version, (b) schema exists but history is absent, (c) history exists but schema effects are absent, or (d) genuinely unapplied. Use catalog evidence and reviewed SQL to establish the classification. **Do not rename already-applied files or mark versions applied/reverted by guess.**
5. Have the database/migration owner approve an explicit version crosswalk before any `migration repair` or SQL application. Supabase documents that repair changes only the migration tracking table; it does not apply or revert SQL. A repair is appropriate only after the live schema is independently verified to match the exact migration effect.
6. Resolve prerequisite role and schema ownership first. The repository base delivery-ledger migration creates `clinic_outreach_runtime` if absent and requires `clinic_runtime`; the #115 migration instead raises if `clinic_outreach_runtime` is absent. The supplied shadow snapshot says the restricted role and delivery ledger are absent. This proposal does not authorize creating the role or applying the base migration.
7. Rehearse the approved sequence on a disposable PostgreSQL database using the exact merged migration files and expected pre-existing roles/catalog. For the clean additive sequence, order is base delivery ledger → identity migration → identity ACL migration → #112 `140000`/`140100`/`140200`/`150000`/`151000` → #115 `160000`. Re-check migration history and catalog after rehearsal.
8. Only after the crosswalk, prerequisites, backup/restore plan, SQL review, and rehearsal are approved should an operator run the exact reviewed migration plan. Capture before/after history and catalog evidence. Stop on any unexpected pending migration or catalog difference; do not retry or repair automatically.

Until these steps are signed off, the `db push` path is blocked. Do not use a blind `supabase db push` against the shadow or production project: its version-based comparison would see repository versions that remote history does not contain and could replay SQL whose equivalent effects are already present under other versions.

Reference: [Supabase Database Migrations — diagnosing history drift and `migration repair`](https://supabase.com/docs/guides/deployment/database-migrations#diagnosing-and-fixing-sync-errors). The project migration-history query was read-only; Supabase CLI was unavailable locally, so no linked CLI status command was run.
