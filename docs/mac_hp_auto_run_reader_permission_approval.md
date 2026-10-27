# Mac HP Auto Run Reader permission proposal (not applied)

The Mac HP Auto Run UI reads its persisted status from `app_config.settings`.
READ ONLY verification against the current Supabase project showed that
`clinic_cloudrun_reader` connects successfully, but lacks `USAGE` on
`app_config` and receives SQLSTATE `42501` for that status query. The HP Writer
can read and write the existing state row. This is a permission issue, not a
connection failure.

The proposal grants the Reader access only to the `key` and `value` columns and
only to the `hp_auto_run_state` row. It does not grant INSERT, UPDATE, DELETE,
sequence access, or access to any other `app_config.settings` row. The existing
HP Writer role and RLS policies are unchanged. This SQL has **not** been run.

```sql
BEGIN;

GRANT USAGE ON SCHEMA app_config TO clinic_cloudrun_reader;
GRANT SELECT (key, value) ON app_config.settings TO clinic_cloudrun_reader;

CREATE POLICY clinic_cloudrun_reader_hp_auto_state_select
ON app_config.settings
FOR SELECT
TO clinic_cloudrun_reader
USING (key = 'hp_auto_run_state');

COMMIT;
```

Rollback, if approved changes later need to be removed:

```sql
BEGIN;

DROP POLICY IF EXISTS clinic_cloudrun_reader_hp_auto_state_select
ON app_config.settings;
REVOKE SELECT (key, value) ON app_config.settings FROM clinic_cloudrun_reader;
REVOKE USAGE ON SCHEMA app_config FROM clinic_cloudrun_reader;

COMMIT;
```

After approval and application by the database owner, verify with a Reader-only
`SELECT value FROM app_config.settings WHERE key='hp_auto_run_state'`, confirm
other keys remain invisible, and confirm `has_table_privilege(...,'INSERT')`,
`UPDATE`, and `DELETE` remain false for the Reader.
