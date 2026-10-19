-- A. 実行前 READ ONLY 診断SQL
-- Supabase SQL Editorで、Migration適用前に実行してください。SELECT文のみで、
-- データ・スキーマへの変更は一切ありません。
-- 1回のRunでまとめて実行すると、結果セットが複数返ります（Editor側で全件確認してください）。

-- 1. 既存ロール
select rolname, rolcanlogin, rolbypassrls, rolsuper
from pg_roles
where rolname in ('clinic_runtime','clinic_outreach_runtime','authenticated','anon','postgres')
order by rolname;

-- 2. outreachスキーマの既存テーブル/ビュー
select table_name, table_type
from information_schema.tables
where table_schema = 'outreach'
order by 1;

-- 3. outreach.email_contact_events の列定義（アプリ側ロールでは見えないため、
--    SQL Editor側＝高権限で確認してください）
select column_name, data_type, is_nullable
from information_schema.columns
where table_schema = 'outreach' and table_name = 'email_contact_events'
order by ordinal_position;

-- 4. outreach.clinic_email_summary の列定義とビュー定義
select column_name, data_type
from information_schema.columns
where table_schema = 'outreach' and table_name = 'clinic_email_summary'
order by ordinal_position;

select pg_get_viewdef('outreach.clinic_email_summary'::regclass, true) as view_definition;

-- 5. outreach.email_delivery_records が既に存在しないことの確認
--    （存在する場合は、本Migrationを適用せず先に状況を確認してください）
select to_regclass('outreach.email_delivery_records') as should_be_null_if_not_yet_created;

-- 6. 既存の行数（0件であることの確認。0件でない場合はSTEP2のバックフィル計画を先に検討）
select count(*) as email_contact_events_rows from outreach.email_contact_events;

-- 7. public.clinics の主キー・行数（参考）
select count(*) as clinics_rows from public.clinics;

-- 8. clinic_outreach_runtimeロールがまだ存在しないことの確認
--    （存在しない=想定どおり。20261016172806マイグレーションが作成する）
select exists(select 1 from pg_roles where rolname = 'clinic_outreach_runtime') as role_already_exists;
