# D. 問題発生時の安全な復旧手順

本番適用は今回実施していません。将来、ユーザー本人がSupabase SQL Editorで
`B_migration.sql`（3本のMigration）を適用する際の復旧手順です。

## 前提

- 3本のMigrationはいずれも `begin; ... commit;` で1トランザクションに包まれている。
- ローカルPostgreSQL（initdb）での検証により、途中で1文でも失敗すると
  **そのファイル全体が自動的にロールバックされ、何も残らないこと**を確認済み
  （`04_rollback_test.sql`での実証：CREATE TABLE・ALTER TABLEが成功した後に
  3文目が失敗しても、成功した2文も含めて全てロールバックされた）。
- したがって、**SQL Editor側で「1ファイルを1回のRunでまとめて実行」していれば、
  失敗時に手動ロールバックは基本的に不要**（Postgres自身が保証する）。

## ケース1：Migration実行中にエラーが出て停止した

**対応不要。** エラーが出た時点でそのトランザクション全体が自動的に
ロールバックされている。`A_readonly_diagnosis.sql`の該当クエリ（再実行可能、
副作用なし）で「適用されていないこと」を確認し、エラー内容を確認してから
再実行してください。

## ケース2：3本のうち1本目・2本目は成功、3本目でエラーが出た

1本目・2本目はそれぞれ別トランザクションとして既にコミット済みのため、
その2本は正常に残る（これは想定どおりの状態で、ロールバック不要）。
3本目だけ、ケース1と同様にエラー時点で自動ロールバックされている。
→ 3本目のエラー内容を確認し、修正してから3本目だけ再実行してください。
（3本はいずれも`drop ... if exists`/`create or replace`/`grant`中心の
再実行安全な設計のため、1本目・2本目を再実行しても害はない）

## ケース3：Migrationは成功したが、アプリ動作で問題が起きた

`outreach.email_delivery_records`への書込は `clinic_outreach_runtime` に
INSERT/UPDATEの列単位GRANTのみで、DELETE権限は付与されていない
（既存Migrationの設計を維持）。誤ったデータが入った場合：

1. **何もせず、まず`C_post_migration_verification.sql`で現状を確認する。**
2. 個別行の訂正が必要な場合は、`postgres`（または十分な権限を持つロール）で
   SQL Editorから直接 `UPDATE outreach.email_delivery_records SET status = ...
   WHERE outreach_id = '<該当id>'` を実行する（`clinic_outreach_runtime`では
   許可されていない列・操作がある点に注意。必要な列は既存Migrationの
   `grant update (...)`一覧を参照）。
3. **`outreach.email_contact_events`への誤った自動記録が入った場合**：
   このテーブルは `clinic_outreach_runtime` から見えない（SELECT/INSERT/UPDATE/DELETEいずれも
   権限なし）ため、`postgres`のみが訂正可能。
   `DELETE FROM outreach.email_contact_events WHERE event_key = '<該当key>'`
   で該当イベント1件のみを削除できる（他の行・他の医院には影響しない）。
4. `outreach.clinic_email_summary`はVIEW（実データを持たない）なので、
   元データ（`email_contact_events`）を訂正すれば自動的に反映される。
   ビュー自体を直接書き換える必要はない。

## ケース4：トリガー（`outreach.record_contact_event()`）を一時的に止めたい

```sql
-- 緊急停止（既存テーブル・データは無傷）
alter table outreach.email_delivery_records
  disable trigger email_delivery_records_record_contact_event;

-- 再開
alter table outreach.email_delivery_records
  enable trigger email_delivery_records_record_contact_event;
```

トリガーを止めても `email_delivery_records` 自体の読み書きには影響しない
（イベントログへの転記だけが止まる）。

## ケース5：3本のMigrationすべてを取り消したい（適用前の状態に戻す）

```sql
-- 20261019140200の取り消し
begin;
revoke select on outreach.clinic_email_summary from clinic_outreach_runtime;
commit;

-- 20261019140100の取り消し
begin;
drop trigger if exists email_delivery_records_record_contact_event
  on outreach.email_delivery_records;
drop function if exists outreach.record_contact_event();
commit;

-- 20261019140000の取り消し（元の制約定義に戻す）
begin;
alter table outreach.email_delivery_records
  drop constraint if exists email_delivery_records_status_check;
alter table outreach.email_delivery_records
  add constraint email_delivery_records_status_check
  check (status in ('DRAFT','SENT','SCHEDULED','REPLIED','FAILED','UNKNOWN','REVIEW'));
commit;
```

いずれも`outreach.email_contact_events`・`outreach.clinic_email_summary`・
`public.clinics`のデータは一切変更しない。
`outreach.email_delivery_records`自体（20261016172806で作成）を削除する手順は
ここに含めていない（既存テーブル削除は今回の方針上、別途明示的な承認が必要）。
