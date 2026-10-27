# Mac 通常版・Comdesk Preview 起動手順

通常版（医院マスター・Maps由来情報・HP調査・営業対象の閲覧とCSV/XLSX出力）は
`./scripts/launch_v2_mac.sh --headless`。Comdesk差分Preview専用版は
`./scripts/launch_v2_mac.sh --comdesk-preview --headless`。
両者は**リポジトリ外**の別Reader設定を参照し、SQLiteへ切り替えません。

## 初回設定（DB管理者が専用Readerを払い出した後）

通常版は `$HOME/.config/clinic-lead/reader.env`、Preview専用版は
`$HOME/.config/clinic-lead/preview-reader.env` を使います。どちらも
`SUPABASE_RUNTIME_DB_URL=postgresql://<dedicated-reader-connection>` の1行だけを保存し、
`chmod 600` にします。資格情報ファイルをリポジトリに置かないでください。

- 本番 `clinic_runtime` のURLや `.supabase-runtime.env.local` を転用しない。
- DB管理者が、専用ログインロール・SELECT許可・必要テーブル/viewのRLS SELECTポリシーを**事前に審査**する。
- テスト用SQLiteやwriterへフォールバックすることはない。

起動前に接続の成否、ロールの管理者属性、全非システムテーブル・シーケンスの更新権限、必要テーブルのSELECT権限、
`public.clinics` の可視データを検査します。不適合なら画面起動前に停止します。通常版は
`CLINIC_READ_ONLY_UI=1`を設定し、画面閲覧とCSV出力だけを提供します。Maps取込、HP/Email調査、Comdesk反映や
Job操作は画面に出さず、Writer専用資格情報・明示操作・HOLD/承認の設計が整うまで使えません。
秘密の接続URLやパスワードは表示しません。

Preview起動は `CLINIC_COMDESK_PREVIEW_ONLY=1` でComdesk差分確認だけに限定します。

## Reader権限とRLSのレビュー案（未適用）

既存 `clinic_runtime` はINSERT/UPDATE権限を持つため、通常版に使用しません。DB管理者は
通常版の起動前検査は投影済み`public.clinic_cloudrun_readonly`、
`public.clinic_email_enrichment`、
`provenance.clinic_cloudrun_comdesk_rows`、`provenance.templates`、
`hp_research.clinic_hp_research`、`treatment.clinic_treatment_research`、
`treatment.clinic_research_status`、`email_research.clinic_email_status_readonly`のSELECTを確認します。
Email画面では状態別件数のみを表示し、メール候補や調査根拠を取得しません。これは画面の読取契約を示す最低限のrelation一覧です。
DB管理者は各列・RLS条件を追加レビューし、Email候補・個人情報・原本JSON・履歴など画面に不要な列/行は
Reader用viewから除外してください。追加relationが必要と判明した場合は、検査リストと投影設計を更新してから起動します。

提案する境界は専用NOINHERIT/NOBYPASSRLS/NOLOGIN helperを作り、helperにのみ必要列SELECTと
`TO <reader_helper>` のSELECTポリシーを与え、専用LOGIN Readerには投影済みviewのSELECTだけを付ける方式です。
Email状態viewは`clinic_id,research_status,candidate_count,researched_at`だけを出し、
`security_invoker=true`で基表RLSを有効にしてください。基表policyは専用Readerに対するSELECTのみとし、
全国医院マスターの利用が承認された対象範囲であることをDBAが確認してから行範囲を定めます。
`USING (true)`は全国全件の開示承認が明示され、他のClinic側RLS条件と矛盾しない場合に限る選択肢です。
実装するview/policyの形（レビュー用のSQL案。実行禁止）:

```sql
CREATE VIEW email_research.clinic_email_status_readonly
  WITH (security_invoker = true) AS
SELECT clinic_id, research_status, candidate_count, researched_at
FROM email_research.clinic_email_status;
-- DBAが全国参照を承認した場合に限り、上記4列のSELECTと
-- FOR SELECT TO <dedicated_reader> USING (true) を個別に付与する。
-- 候補テーブルへのSELECTは不要。
```

画面要件が未確定の参照先については、権限を追加せず起動を拒否し、
不足relation/columnと必要なSELECT/RLS案を管理者レビューへ返します。本番GRANT・Policy作成はこの作業では行いません。

## DB作業（保留）

既存Readerの資格情報・権限は未確認で、通常版の全参照先SELECTとRLSは未確定です。
特にMaps/HP/Email関連のソーステーブルを直接公開するのではなく、画面に必要な投影列と行範囲を先に確定してください。
**本番DBのCREATE ROLE / GRANT / CREATE POLICY / ALTER ROLEは未実施**。

レビュー事項：
1. Comdeskプレビューの対象医院を `provenance.comdesk_preview_scope` 等の許可リストで限定するか。
2. 関連テーブルのRLSポリシーを同一の許可リストでスコープ制限できるか。
3. 医院マスター全件表示とComdeskプレビューの最小権限を同じロールにまとめてよいか（原則分離）。
4. Readerのパスワード発行・ローテーション・失効方法を確定する。

**このPRはDBロールを作成せず、本番データの参照可能範囲も変更しない。**

## 起動が拒否される例

- 設定がない、またはファイルの権限が600ではない
- 従来の `clinic_runtime` や書き込みできる資格情報を指定している
- 対象のテーブルにSELECT/RLSの閲覧権限がない
- ネットワークに接続できない
- Readerから見える医院データが0件

上記は意図したfail-closed動作であり、Writerを設定して無理に起動してはいけない。
