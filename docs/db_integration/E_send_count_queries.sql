-- E. 送信回数・下書き回数集計SQL（参考、READ ONLY）
-- Migration適用後、outreach.clinic_email_summary（既存ビュー、無変更）で
-- そのまま取得できます。新規ビューは作成していません。

-- 医院ごとの下書き回数・送信回数・最終送信日時（全キャンペーン横断）
select clinic_id, clinic_name, draft_count, sent_count, has_sent, last_sent_at, latest_event
from outreach.clinic_email_summary
order by last_sent_at desc nulls last
limit 100;

-- 送信済みが1件もない医院（新規キャンペーンの対象候補の参考）
select clinic_id, clinic_name, draft_count
from outreach.clinic_email_summary
where has_sent = false
order by clinic_id;

-- クロスキャンペーンでの重複候補（PR #103のclinic_statuses()と同じ考え方：
-- 複数キャンペーンに渡って記録がある医院）
select clinic_id, count(*) as campaign_count, array_agg(distinct campaign_id) as campaigns
from outreach.email_delivery_records
group by clinic_id
having count(*) > 1
order by campaign_count desc;

-- 直近のイベント種別ごとの件数（運用ダッシュボード向け）
select event_type, count(*) from outreach.email_contact_events group by 1 order by 1;
