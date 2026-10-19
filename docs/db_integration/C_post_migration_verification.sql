-- C. 実行後の検証SQL
-- B（3本のMigration）を適用した直後にSupabase SQL Editorで実行してください。
-- 全てSELECT文のみ（最後の「機能テスト」ブロックのみ、最後にROLLBACKするテスト用の
-- INSERT/UPDATEを含みます。本番データを一切残しません）。

-- 1. テーブル・ビュー・制約の存在確認
select to_regclass('outreach.email_delivery_records') as delivery_records_exists;
select conname, pg_get_constraintdef(oid) as definition
from pg_constraint
where conname = 'email_delivery_records_status_check';
select tgname from pg_trigger
where tgname = 'email_delivery_records_record_contact_event';

-- 2. ロール・権限の確認
select rolname from pg_roles where rolname = 'clinic_outreach_runtime';
select grantee, privilege_type
from information_schema.role_table_grants
where table_schema = 'outreach' and table_name = 'clinic_email_summary'
order by 1, 2;

-- 3. RLSが維持されていること（既存テーブルを破壊していないこと）
select relname, relrowsecurity, relforcerowsecurity
from pg_class c join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'outreach'
  and relname in ('email_delivery_records','email_contact_events')
order by 1;

-- 4. 既存の行数が変化していないこと（Migration自体はデータを書き込まない）
select count(*) as email_contact_events_rows_should_be_unchanged
from outreach.email_contact_events;

-- 5. 機能テスト（実データ・実医院は使わず、トランザクション内で作成しロールバック）
--    このブロック全体を1回のRunでまとめて実行してください。
begin;

set role clinic_outreach_runtime;

-- 5a. 存在しないテスト用clinic_idで試す場合はpublic.clinicsのFKに失敗するので、
--     実際に1件存在するclinic_idに書き換えてから実行してください。
--     <<<ここにテストに使う実在のclinic_idを入れてください>>>
-- 例: select id from public.clinics limit 1;

-- insert into outreach.email_delivery_records
--   (operation_id, idempotency_key, clinic_id, clinic_uuid, clinic_name_snapshot,
--    recipient_email, sender_account, staff_name, campaign_id, generation_batch_id,
--    status, listing_status, article_type, subject, body_sha256)
-- values
--   (gen_random_uuid(), 'verification-test-key', <clinic_id>, 'test-uuid',
--    'Verification Test Clinic', 'clinic@example.test', 'sender@example.test',
--    'verification', 'verification-campaign', gen_random_uuid(),
--    'REVIEW', 'NOT_LISTED', 'NORMAL', 'verification subject', 'verification-sha');

-- update outreach.email_delivery_records set status='SENT', sent_at=now()
--   where idempotency_key = 'verification-test-key';

-- -- 同じSENTへの再同期は二重加算されないことを確認
-- update outreach.email_delivery_records set status='SENT', sent_at=now()
--   where idempotency_key = 'verification-test-key';

reset role;

-- select event_type, count(*) from outreach.email_contact_events
--   where campaign_id = 'verification-campaign' group by 1;
-- -- 期待結果: HOLD=1, SENT=1 のみ（SENTが2件になっていないこと）

rollback; -- テストデータは一切残さない
