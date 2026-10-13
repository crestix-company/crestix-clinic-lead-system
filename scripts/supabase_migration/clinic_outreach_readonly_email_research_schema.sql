-- READ ONLY access to Email Research data for the 「メール送付文章」 outreach quality
-- verification (src/outreach/readonly_access.py 系とは別枠; メールアドレス結合専用).
--
-- Prepared offline for DBA review. DO NOT apply to Production (hwdvyhunozporfuefteb /
-- clinic-lead-shadow) automatically -- this file is not run by any script in this repo and
-- must be applied manually (e.g. via the Supabase SQL editor) by someone with migration
-- authority, after review.
--
-- BACKGROUND (read-only inspection of the live schema; no data rows read, no writes):
--   * email_research.clinic_email_status / email_candidates / email_auto_runs /
--     email_rejection_evidence は RLS enabled. email_research.clinic_targets は VIEW
--     (RLSはテーブル側に委譲、ビュー自体への独立したSELECT grantが必要)。
--   * 既存の `clinic_email_runtime` ロールは email_research.* に INSERT/UPDATE を含む
--     書き込み権限を持つ Production稼働中ロールであり、READ ONLY検証用途での
--     再利用は禁止(今回の方針: 既存ロールの書き込み権限を一切経由しない)。
--   * 既存の `clinic_readonly` ロール(本ファイルのGRANT対象)は、現時点で
--     email_research.* への権限を一切持たない(確認済み、0件)。
--   * clinic_id (bigint) で public.clinics.id と結合可能。Clinic UUIDへの変換は
--     呼び出し側で public.clinics をJOIN/再取得して行う(既存のREAD_TABLES内で完結)。
--
-- Safe-by-construction properties:
--   * SELECT only. No INSERT/UPDATE/DELETE/TRUNCATE, no DDL, no BYPASSRLS.
--   * email_auto_runs / email_rejection_evidence (運用ログ・判定根拠ログ) は対象外
--     -- 今回の検証は「使えるPrimary Emailがあるか」の確認のみで、ログ閲覧は不要なため
--     最小権限の原則で含めない。
--   * clinic_readonly の既存権限(public.clinics / hp_research.clinic_hp_research)には
--     一切変更を加えない。
--   * 他ロール(clinic_runtime, clinic_email_runtime)には一切変更を加えない。
--   * Idempotent (IF NOT EXISTS / DO $$ catalog checks) -- safe to re-run.

begin;

grant usage on schema email_research to clinic_readonly;

grant select on table email_research.clinic_email_status to clinic_readonly;
grant select on table email_research.email_candidates to clinic_readonly;
grant select on table email_research.clinic_targets to clinic_readonly;  -- VIEW

do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname = 'email_research' and tablename = 'clinic_email_status'
      and policyname = 'clinic_readonly_select'
  ) then
    create policy clinic_readonly_select
      on email_research.clinic_email_status
      for select
      to clinic_readonly
      using (true);
  end if;
end $$;

do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname = 'email_research' and tablename = 'email_candidates'
      and policyname = 'clinic_readonly_select'
  ) then
    create policy clinic_readonly_select
      on email_research.email_candidates
      for select
      to clinic_readonly
      using (true);
  end if;
end $$;

-- email_research.clinic_targets is a VIEW; RLS is not applicable to the view itself, only
-- the grant above is required (relrowsecurity=false confirmed for this relation).

revoke insert, update, delete, truncate on
  email_research.clinic_email_status, email_research.email_candidates
  from clinic_readonly;

commit;

-- Read-only verification after a separately approved apply:
--
-- select table_schema, table_name, privilege_type from information_schema.role_table_grants
--   where grantee = 'clinic_readonly' and table_schema = 'email_research' order by 1,2,3;
-- -- expected: exactly SELECT on clinic_email_status, email_candidates, clinic_targets.
--
-- -- End-to-end (run as clinic_readonly, via its own connection string, never as a superuser):
-- select count(*) from email_research.clinic_email_status;   -- expect > 0
-- select count(*) from email_research.email_candidates;      -- expect > 0
-- insert into email_research.email_candidates (clinic_id) values (-1);  -- expect permission denied

-- ROLLBACK (if this needs to be reverted after apply):
-- begin;
-- drop policy if exists clinic_readonly_select on email_research.clinic_email_status;
-- drop policy if exists clinic_readonly_select on email_research.email_candidates;
-- revoke select on email_research.clinic_targets from clinic_readonly;
-- revoke select on email_research.email_candidates from clinic_readonly;
-- revoke select on email_research.clinic_email_status from clinic_readonly;
-- revoke usage on schema email_research from clinic_readonly;
-- commit;
-- -- This only removes the email_research extension added by this file. It does not touch
-- -- clinic_readonly's existing public.clinics / hp_research.clinic_hp_research access, and
-- -- does not touch clinic_runtime or clinic_email_runtime.
