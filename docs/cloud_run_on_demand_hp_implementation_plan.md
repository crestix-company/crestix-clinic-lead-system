# Clinic Lead System：Google Cloud Run + オンデマンドHP調査 実装計画（2026-10-19）

## 0. 決定事項と非目標
- 月額0円を**目標**とする。Google Cloud無料枠は超過請求の可能性があり、0円の保証ではない。
- HP自動調査を24時間365日実行しない。**Web画面で明示的に開始したときだけ** Cloud Run Jobsが稼働する。
- Streamlitの既存「かんたん操作」を維持。Cloud Run Service（Web、最小0・最大1）と Cloud Run Job（Worker、1タスク・並列度1・0リトライ）を分離する。
- DBは現行SupabaseがSSOT。ローカルSQLite、HP Researchのsidecar、ローカルの認証ファイル、既存Mac上のジョブは勝手に移行・破棄しない。
- 本ドキュメントは**設計のみ**。Production DB書込、DDL/Migration、HP/Email Job操作、Gmail送信・下書き実作成、GCPリソース作成、PRマージを含まない。
- 初期クラウド版は社内向けREAD ONLY画面 → 小規模HP Job Canary → 限定本番化の順番。PR #98/#99/#100は別途レビュー対象。

## 1. 現行コードを利用する範囲
- app_v2.py：Streamlit入口。「かんたん操作」のHP開始/停止/再開・進捗表示あり。
- src/master/hp_auto_run.py：AutoHpRunnerは同じStreamlitプロセス内でdaemon Threadを起動する。Cloud Run Serviceへそのまま持ち込まない。
- src/repository/hp_auto_run_repository.py：Supabaseの永続的AutoRun状態、DB advisory lock、controller lease（120秒）、heartbeat等を再利用できる。
- src/master/jobs.py：既存のHP Job作成・実行・pause/resumeを再利用する。単発Jobは create_single_job() と run_job() を確認する。
- src/repository/runtime_store.py：SupabaseRuntimeStore、読取の再接続処理を再利用。
- scripts/launch_v2_mac.sh、scripts/launch_v2.py：Mac用ロックと8502固定のためCloud Runでは使わない。
- src/outreach/gmail_oauth.py：現状はローカルOAuthファイルを要する。初期クラウドではGmail保存OFFのまま。

## 2. 設計構成
社内ブラウザ --Google IAP--> Cloud Run Service (clinic-lead-web)
  |-- SELECT専用DBロール --> 既存Supabase (Clinic Master / HP進捗)
  |-- JOB開始権限のみ --> Cloud Run Job (clinic-lead-hp-worker)
                                     |-- 専用最小書込DBロール --> 既存Supabase
                                     |-- 既存run_job()で25〜50医院を調査
                                     |-- 完了後終了、Idle時に料金を発生させない設計
- Cloud Scheduler、always-on Worker、公開認証なしURL、常駐バックグラウンドThreadは不要。
- Cloud Run Serviceは 1 vCPU / 1GiB、min 0、max 1、リクエスト課金。無料枠内を目指す。
- Jobは 1 vCPU / 1GiB、tasks 1、parallelism 1、max_retries 0、task_timeout 1800s (30分)。最初は25件でCanary。
- Cloud RunのStreamlit WebSocketが開いている間は課金対象のリクエストが継続しうる。ブラウザを長時間開きっぱなしにしない運用を推奨。

## 3. 開発PRの切り分け

### PR A：クラウド検証版（本番DB READ ONLY）
追加：
- Dockerfile.cloudrun：Python 3.12 slim、requirements.txt、非root、CMDはStreamlit 0.0.0.0:$PORT。
- .dockerignore：.env、.secrets、.supabase-runtime.env.local、config/email_research.env.local、dataの生成物、artifacts、SQLite、CSV実データ、backups、.git等を必ず除外。設定用YAMLや必要な静的リソースは必要に応じてイメージに含める。
- scripts/verify_cloud_runtime.py：秘密値を表示せず、環境変数／DB接続／SELECT専用権限の確認。
- tests/test_cloud_run_config.py：機密情報未同梱、0.0.0.0:$PORTでの起動設定、READ ONLY画面をチェック。
既存画面は原則変更しない。production_runtime.envファイルはCloud Runの起動時に直接は読み込まれないので、環境変数を明示する。

デプロイ先GCPプロジェクトは新規の専用プロジェクトを使用。Cloud Run/IAP/Artifact Registry/Cloud Build/Secret Manager APIを必要最小限で有効化。
- 同一Google Cloudプロジェクト内のWeb用 service account と Job用 service account を分離。
- SupabaseはWeb専用のSELECT権限DBロールを使用。URLはSecret Managerへ。アプリ表示に資格情報を出さない。
- Cloud Run Securityは Require authentication + IAP を設定。社内許可アカウントのみ IAP HTTPS Resource Accessor を付与。初回はConsoleで設定が必要な場合あり。
- 検証版は CLINIC_READ_ONLY_UI=true / CLINIC_DATA_BACKEND=supabase / CLINIC_WRITE_BACKEND=supabase / OUTREACH_GMAIL_DRAFTS_ENABLED=false / EMAIL_AUTO_RUN_ENABLED=false。
- IAP未設定での公開（allow unauthenticated）は禁止。
- 確認項目：IAP未許可ユーザーはアクセス不可、許可ユーザーは起動可、営業対象件数・HP進捗のSELECT可、全Job操作無効。
- 専用READ ONLY DBロールがSELECT専用であることを検証。画面のREAD ONLYフラグだけでは権限制限にならない。

### PR B：Cloud Run HP Worker 単発小規模調査
追加予定：
- scripts/cloud_hp_worker.py：単発HP Job IDをCloud Run Executionの環境変数から取得し、検証済みIDに対して既存の run_job() を**同期処理**で実行。HTTPプロセスやdaemon Threadは起動しない。
- src/cloud_run/job_dispatch.py：Google認証付きJobs APIのrun呼出しと結果を検証する境界。オーバーライドで渡すのはジョブIDだけ。秘密情報をオーバーライドさせない。
- tests/test_cloud_hp_worker.py：対象外Job拒否、二重起動防止、停止中/完了中Jobを実行しない、未確認Executionの再試行禁止、SIGTERM/timeoutからの安全な再開。
- Job実行用Supabaseロールは既存の権限境界を確認し、research Jobと完了結果の必要最小限のみ。権限新規作成・Migrationは別承認。
- DB上のJobレコード/Clinic ID/状態を必ず照合。workerの終了時は結果と未完了件数をDBから取得し、ローカルファイルにのみ保持しない。
- Cloud Run Jobs自体の「parallelism 1」は**1実行内**の制限。別のExecutionが同時に起動しうるため、DBのlease/unique/ジョブ状態で**実行間**の二重起動も抑止。
- WebサービスはCloud Run Jobの起動（jobs.run with env override）だけ許可。Job設定更新・削除権限は不要。専用サービスアカウントにジョブ単位で最小権限付与。
- HTTPタイムアウトによる起動結果不明時は、DBにDISPATCH_UNKNOWNとして記録し、Executionを照合するまでは再発行しない。
- 完了済みは再調査せず、必要なら別途明示的なforce承認を要求。
- 初期版では「未調査がなくなるまで連続調査」モードは使わず、指定件数の単発25件 → 50件まで。

### PR C：Streamlit操作画面のCloud Run対応
- app_v2.pyの既存UIは維持し、CLINIC_HP_EXECUTOR=cloud_runのときのみローカルrunner.start() / auto_runner.start() を使わずjob_dispatchへ委譲。
- 設定対象: prefecture, 医科/歯科, 25/50件, 調査済みやり直し=false。
- 「開始」前に、認証済み社内ユーザー、Jobが未実行、履歴が照合済み、件数上限内、月予算ゲートを確認。
- 「開始」：既存 create_single_job() が生成したJob IDを渡す。Cloud Run APIでExecution生成したらexecution nameを保存する。
- 「停止」：DB上でpause_requestedを保存し、現行run_jobが安全な医院境界で停止。即時停止は原則しない。
- 「再開」：DB残件数・Lease・Execution状況を照合して、**新しいExecutionを明示的に起動**。無断の自動再開なし。
- 「進捗」：Supabaseの状態とCloud Run Execution状態を表示（RUNNING/PAUSED/COMPLETED/ERROR/UNKNOWN）。
- Streamlit rerun、複数タブの二重クリック、Cloud Run再起動、ネットワークタイムアウトでも1つのJobを重複作成・多重実行しない。
- クラウド版では既存Local AutoRunを勝手に復旧・再開しない。

### PR D：受入試験／限定リリース
順番：
1. 既存Jobは触らず、モックJobでUI/重複判定を試す。
2. 承認済み検証用データだけで5医院Canary（専用Job/ロール/費用管理を確認）。
3. 25医院→50医院の順で拡大。重複0、既存HPデータ破壊0、途中停止・再開成功。
4. Cloud Runブラウザを閉じても実行中Jobは継続。Job終了後はゼロ実行。
5. 低頻度運用のCloud Billing請求明細を確認。月額0円を保証せず、問題があればJob起動を遮断。
6. mainマージ・本番DBへのアクセス解禁・既存Macからの切替は個別承認。

## 4. GCP構築手順（実行コマンド例、プレースホルダー）
- GCP_PROJECT_ID、許可するGoogleアカウント、専用のDB READ ONLY URLを人間が確定する。
- gcloud auth login
- gcloud config set project GCP_PROJECT_ID
- gcloud services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com secretmanager.googleapis.com iap.googleapis.com
- Artifact Registry: 東京リージョンにDockerリポジトリを1つ作成。古いイメージは清掃し容量を抑える。
- Dockerfile.cloudrun からコンテナビルド、イメージのSHA digestを記録。
- Secret Managerで clinic-lead-readonly-db-url を作成。シークレット値をターミナル/PR/ログに出さない。閲覧権限はWebのSAだけ。
- Cloud Run clinic-lead-preview を min 0、max 1、IAP、READ ONLY、シークレット参照でデプロイ。IAM許可ユーザーのログインとDB SELECT動作を確認。
- PR B/Cの検証と承認後、Cloud Run Job clinic-lead-hp-worker を tasks 1、parallelism 1、retries 0、timeout 1800sで作成。
- JOB作成のみで実行しない。Job起動は別途5医院Canaryの明示承認で1回だけ。
- Cloud Schedulerは作らない。

## 5. コスト監視の必須条件
- Cloud Run Service min 0 / max 1、Job 1タスク・0リトライ・30分上限。
- 同時ExecutionはDB上1件だけ。Webボタン連打でも新しいJobが増えない。
- 予算通知は0円停止を保証しない。Cloud Billingのspend cap機能の適用可否を当該サービスで調べ、適用できないサービスはアプリ独自の件数/時間/回数制限で保護。
- WebSocketを開きっぱなしにすると課金対象のリクエストが継続する。Cloud Runの無料枠をジョブ専用と混同しない。
- Cloud Build、Artifact Registry（0.5GiB月まで無料）、Secret Manager、Cloud Logging、外部データ転送、Tavily等も費用監視対象。
- Cloud Schedulerと高額なロードバランサーは初期構成で使わない。Cloud Runに直接IAPを付ける。

## 6. 合否ゲートとロールバック
検証合格の必要条件：
- ローカル ClinicLeadMain の動作不変、main未変更。
- 既存の全pytestが通過、クラウド専用の追加テストも全PASS。
- READ ONLY DBロールでDB更新不可、未認証ユーザーはWeb利用不可。
- Web側の権限では本番HPデータへ書き込めない。
- UI起動だけでHP Job開始ゼロ、IAP認証だけでJob開始ゼロ、Cloud Schedulerなし。
- 5医院CanaryでJob実行数1、二重claim 0、完了結果永続化、UIによる進捗反映、停止・再開成功。
- 事前承認なしのProduction Migration、Gmail下書き実作成、送信、旧ジョブ再開ゼロ。

障害時：
- Cloud RunのWeb/Job側の実行権限を無効化し、新規Job投入を停止。
- Supabase永続状態を監査し、完了済みClinic IDと残件数を照合。
- Macの現行運用は維持し、勝手に旧ジョブを再開しない。
- DB状態不一致はHOLDにし、人間の確認後に再開する。

## 7. 公式ドキュメント
- https://cloud.google.com/run/docs/securing/identity-aware-proxy-cloud-run
- https://cloud.google.com/run/docs/execute/jobs
- https://cloud.google.com/run/docs/create-jobs
- https://cloud.google.com/run/docs/configuring/jobs/secrets
- https://cloud.google.com/run/docs/triggering/websockets
- https://cloud.google.com/run/pricing
- https://cloud.google.com/artifact-registry/pricing
