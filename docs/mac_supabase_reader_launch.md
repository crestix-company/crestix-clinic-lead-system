# Mac Supabase Reader 起動手順

Comdesk Preview専用のMac起動は `./scripts/launch_v2_mac.sh --headless`。
GitHub worktreeを切り替えても同じ**リポジトリ外**のReader設定を参照します。

## 初回設定（DB管理者が専用Readerを払い出した後）

- 本番 `clinic_runtime` のURL、`.supabase-runtime.env.local` の内容は転用しない。
- `$HOME/.config/clinic-lead/preview-reader.env` を作り、内容は
  `SUPABASE_READONLY_DB_URL=postgresql://<dedicated-reader-connection>` の**1行のみ**とする。
- `chmod 600 "$HOME/.config/clinic-lead/preview-reader.env"` を実行。
- 通常版用に別ファイル `$HOME/.config/clinic-lead/runtime-writer.env` を用意し、
  `SUPABASE_RUNTIME_DB_URL=postgresql://<existing-writer-connection>` を設定して `chmod 600` にする。
- ReaderとWriterを同じ接続にしない。既存のリポジトリ内 `.supabase-runtime.env.local` は自動コピー・変更しない。
- DB管理者が、専用ログインロール・SELECT許可・必要テーブルのRLS SELECTポリシーを**事前に審査**する。
- テスト用SQLiteやwriterへフォールバックすることはない。

`./scripts/launch_v2_mac.sh --headless` は接続の成否、ロールの管理者属性、非システムテーブル・シーケンスへの更新権限、必要な7テーブルのSELECT権限、`public.clinics` の可視データの有無を検査し、不適合なら画面起動前に停止する。秘密の接続URLやパスワードは表示しない。Reader URLは専用の `SUPABASE_READONLY_DB_URL` だけに渡し、Writer URLはUIプロセスに渡さない。

HP Jobを操作する通常版では `./scripts/launch_v2_mac_normal.sh` を使用する。通常版も同じ専用Readerファイルを検証したうえで、ReaderとWriterを異なる環境変数・接続として起動する。通常版Readerには上記7テーブルに加えて `research.research_jobs`、`research.research_job_items`、`app_config.settings` のSELECT権限が必要で、起動前に確認する。通常版Writerはリポジトリ外のmode-600ファイルから読み込み、両DSNが同一なら起動しない。画面起動だけではJobやWorkerは開始しない。Comdesk PreviewはWriter設定を読み込まず、HP Job操作を提供しない。

プレビュー起動は `CLINIC_COMDESK_PREVIEW_ONLY=1` でComdesk差分確認の機能制限を維持する。通常運用の調査・DB更新・Gmail下書き作成に利用しない。

## DB作業（保留）

現時点の既存Readerロールは必要テーブルのSELECT権限が揃っていない。特に`provenance.google_maps_results` と`email_research.email_candidates` を含むRLSポリシー設計が未確定。**本番DBのCREATE ROLE / GRANT / CREATE POLICY / ALTER ROLEは未実施**。

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
