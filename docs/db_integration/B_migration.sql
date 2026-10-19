-- B. 必要なMigration SQL（適用順）
-- Supabase SQL Editorで、以下の順に実行してください。
--
-- 0) supabase/migrations/20261016172806_outreach_email_delivery_records.sql
--    （既存ファイル。無変更で適用可能。ローカルPostgreSQLで、現行の
--     outreach.email_contact_events / outreach.clinic_email_summary が
--     既に存在する状態を再現したうえで検証済み：競合なし。）
--
-- 1) supabase/migrations/20261019140000_outreach_email_delivery_records_trashed_status.sql
-- 2) supabase/migrations/20261019140100_outreach_contact_events_bridge.sql
-- 3) supabase/migrations/20261019140200_outreach_clinic_email_summary_grant.sql
--
-- 以下は1)〜3)をこの順でまとめて貼り付けられるようにした内容（ファイル本体と同一）。
-- 1ファイルとして1回のRunで実行してください（途中で失敗した場合、各begin/commitの
-- 単位で自動的にロールバックされます。詳細はD_recovery_procedure.mdを参照）。

-- ============================================================
-- 1) TRASHED状態を追加
-- ============================================================
begin;

alter table outreach.email_delivery_records
  drop constraint if exists email_delivery_records_status_check;
alter table outreach.email_delivery_records
  add constraint email_delivery_records_status_check
  check (status in ('DRAFT','SENT','SCHEDULED','REPLIED','TRASHED','FAILED','UNKNOWN','REVIEW'));

commit;

-- ============================================================
-- 2) email_delivery_records -> email_contact_events への自動転記
-- ============================================================
begin;

create or replace function outreach.record_contact_event()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, outreach
as $$
declare
  v_event_type text;
  v_key text;
begin
  v_event_type := case new.status
    when 'DRAFT' then 'DRAFT_CREATED'
    when 'SENT' then 'SENT'
    when 'SCHEDULED' then 'SCHEDULED'
    when 'REPLIED' then 'REPLIED'
    when 'TRASHED' then 'TRASHED'
    when 'REVIEW' then 'HOLD'
    when 'UNKNOWN' then 'HOLD'
    else null
  end;
  if v_event_type is null then
    return new;
  end if;
  if tg_op = 'UPDATE' and old.status = new.status then
    return new;
  end if;
  v_key := new.outreach_id::text || ':' || v_event_type;
  insert into outreach.email_contact_events
    (clinic_id, campaign_id, event_key, event_type, gmail_message_id, gmail_thread_id,
     gmail_draft_id, occurred_at)
  values
    (new.clinic_id, new.campaign_id, v_key, v_event_type, new.gmail_message_id,
     new.gmail_thread_id, new.gmail_draft_id, coalesce(new.sent_at, new.updated_at, now()))
  on conflict (event_key) do nothing;
  return new;
end;
$$;

drop trigger if exists email_delivery_records_record_contact_event
  on outreach.email_delivery_records;
create trigger email_delivery_records_record_contact_event
after insert or update on outreach.email_delivery_records
for each row execute function outreach.record_contact_event();

commit;

-- ============================================================
-- 3) アプリ用ロールにclinic_email_summaryの参照権限を付与
-- ============================================================
begin;

grant usage on schema outreach to clinic_outreach_runtime;
grant select on outreach.clinic_email_summary to clinic_outreach_runtime;

commit;
