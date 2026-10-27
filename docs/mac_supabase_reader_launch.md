# Macローカル起動モード

このブランチはPR #145の閲覧基盤、PR #146のHP Job排他・安全制御、PR #147の通常HP UIを統合する作業用ブランチです。PR #145と#146はmainから分岐しているため、単独で順番にマージせず、統合後のPR #147をレビュー対象にします。いずれもDraftのままです。

起動モードは3つです。リポジトリルートから実行してください。

| 用途 | コマンド | 外部設定 | DB経路 |
| --- | --- | --- | --- |
| READ ONLYの医院閲覧・営業一覧・出力 | `./scripts/launch_v2_mac.sh --headless` | `$HOME/.config/clinic-lead/reader.env` | Readerのみ |
| Comdesk Preview | `./scripts/launch_v2_mac.sh --comdesk-preview --headless` | `$HOME/.config/clinic-lead/preview-reader.env` | Preview Readerのみ |
| HP調査通常版 | `./scripts/launch_v2_mac_normal.sh --headless` | 上記Reader、`runtime-writer.env`、`hp-provider.env` | ReaderとHP Writer |

全モードはローカル127.0.0.1にbindし、SQLiteへfallbackしません。同じリポジトリのUIは共有file lockで同時起動を拒否します。READ ONLYとPreviewはWriter資格情報をUIプロセスへ渡しません。HP通常版ではHP操作だけがWriterを使います。Streamlitプロセス自体にはWriter DSNが存在するため、プロセス内の侵害に対する資格情報隔離ではありません。HP通常版のUIからHP以外のWriter操作へ到達できないことと、バックエンドのWriter呼び出しがHP用途に限定されることを回帰テストで確認します。

## 外部設定ファイル

初回は秘密値を含まないテンプレートを手元で作成し、DB管理者から払い出された専用接続URLを各ファイルに設定してください。既存の設定ファイルは表示・変更しません。権限は各ファイルを作成した後に`chmod 600`で設定します。

```sh
mkdir -p "$HOME/.config/clinic-lead"
chmod 700 "$HOME/.config/clinic-lead"
for name in reader.env preview-reader.env runtime-writer.env hp-provider.env; do
  path="$HOME/.config/clinic-lead/$name"
  if [ ! -e "$path" ]; then (umask 077; : > "$path"); fi
  chmod 600 "$path"
done
```

この作成例は既存ファイルを上書きしません。各ファイルに権限者から取得した値を安全なエディタで記入してください。`reader.env`にはDB管理者から提供される`clinic_cloudrun_reader`専用DSNを、`SUPABASE_READONLY_DB_URL=...`の1行で保存します。Writer DSNからReader URLを作らず、Writerのrole名やパスワードを流用しないでください。`preview-reader.env`も専用のReader URLだけ、`runtime-writer.env`はHP通常版専用で`SUPABASE_RUNTIME_DB_URL=...`のみです。`hp-provider.env`は`TAVILY_API_KEY=...`のみを保存します。パスワード、接続URL、APIキーをコマンド引数、シェル履歴、Git管理ファイルへ置かないでください。起動スクリプトは秘密値を表示しません。

## Readerの最小参照範囲

2026-10-27に対象`clinic-lead-shadow`のカタログをSupabase管理接続からSELECTのみで監査しました。Macの`runtime-writer.env`は接続先プロジェクト識別子の一致確認にだけ使い、そのDSNでは接続していません。接続先は`clinic-lead-shadow`と一致しました。Reader接続には使っていません。

READ ONLY閲覧画面は投影relationを使用します。Comdesk Previewは別ReaderとPreview固有relationを使用します。HP通常版の対象検索・結果・Job進捗で実際に使うrelationは次の4つです。監査対象として確認した`app_config.settings`は現行HP専用画面から参照されないため、権限要件から除外しています。

- `public.clinic_cloudrun_readonly` — 対象抽出と候補表示に使う11列だけ
- `hp_research.clinic_hp_research` — 対象済み判定に使う`clinic_id`だけ
- `research.research_jobs` — `id,kind,status,created_at`
- `research.research_job_items` — `job_id,clinic_id,state,result`

Email候補本文・候補行を読む`email_research.email_candidates`への直接権限は不要です。READ ONLY閲覧・営業一覧には別のrelation契約を使います。現行HP専用画面のReader要件へ混在させません。

カタログ上、医院Viewは`security_barrier=true`の所有者権限Viewで、所有者`clinic_cloudrun_projection_owner`はNOLOGIN/NOBYPASSRLSです。基表`public.clinics`はRLS有効で、projection owner用SELECT policyの条件は`true`です。View所有者には投影に必要な列だけSELECTが付いています。HP台帳もRLS有効で、`clinic_cloudrun_reader`用SELECT policyは`true`、同roleの列権限は限定されています。従ってこれらのpolicyは全国行を返す設計です。全国医院データの利用承認範囲と一致することをDB管理者が確認してください。`research`のReader policyは現状ありません。これはカタログ監査結果であり、今回DDL/GRANT/Policy変更は行いません。

起動時のpreflightは接続roleの管理属性、書込・シーケンス権限、relation/必要列のSELECT、医院行の可視性を検査します。権限が不足した場合は停止します。`clinic_runtime`は書込権限を持つためReaderとして使えません。実データ件数の期待値は環境・RLSに依存します。PRレビューで示された164201件は受入確認時の参照値であり、コードの固定条件ではありません。

## HP通常版の安全境界

起動だけではJob作成・Worker開始をしません。対象検索・件数・候補プレビューと進捗はReader経由です。Job登録とWorker開始は別の明示操作で、HP Writer経路だけを使います。Auto Run、Maps取込、Email調査、Comdesk反映、Gmail操作は通常HP画面から呼び出しません。既存PAUSED Jobを自動で再開・変更しません。実DBでのJob登録・Worker開始には別途承認が必要です。

## DB管理者レビュー用SQL案（実行禁止）

READ ONLYカタログ監査では`clinic_cloudrun_reader`が医院投影ViewのSELECTと`hp_research.clinic_hp_research.clinic_id`を含む列SELECTを持つことを確認しました。同roleはログイン可能で、superuser/createdb/createrole/BYPASSRLS属性はありません。`rolinherit=true`ですがrole membershipはなく、継承権限は確認されませんでした。対象relationへの書込権限もありません。HP台帳には同role向けSELECT policyがあります。`clinic_readonly`は原本`public.clinics`の`base_json`/`effective_json`等、画面に不要な広い列を読めるため選びません。`clinic_runtime`は対象表をINSERT/UPDATEできるためReaderに使いません。

不足するのは`research` schema USAGE、Job一覧・進捗に必要な列SELECT、および両Job relationのHP限定SELECT policyです。`app_config.settings`は現行画面で参照せず、権限を追加しません。下記は既存`clinic_cloudrun_reader`へ足す案で、実行してはいけません。`research_jobs`はHP Jobだけ、`research_job_items`はHP Jobに属するitemだけを可視にします。

適用前に、対象role/relation、現行RLS、policy名の衝突がないことを管理者が再確認します。現在は両research tableでRLS有効、FORCE RLS無効、既存policyは`clinic_runtime`のALL policyのみで、`clinic_cloudrun_reader`用policy名はありません。VIEW所有者・医院projectionのGRANT/RLSは変更対象外です。`clinic_cloudrun_reader`は既存ログインroleのため、下記変更は同じroleを使う他プロセスにもHP Jobの列・行参照を追加します。Cloud Runにも当該資格情報を使っている場合、このスコープ追加をDBAが確認してから判断してください。

変更案を適用する場合は一つのtransactionで実行し、失敗時は全体をrollbackします。現時点では実行禁止です。

```sql
-- 例示のみ。実行禁止。医院投影ViewとHP台帳の既存権限は変更しない。
BEGIN;
GRANT USAGE ON SCHEMA research TO clinic_cloudrun_reader;
GRANT SELECT (id, kind, status, created_at)
  ON research.research_jobs TO clinic_cloudrun_reader;
GRANT SELECT (job_id, clinic_id, state, result)
  ON research.research_job_items TO clinic_cloudrun_reader;
CREATE POLICY clinic_cloudrun_reader_hp_jobs_select
  ON research.research_jobs FOR SELECT TO clinic_cloudrun_reader
  USING (kind = 'hp');
CREATE POLICY clinic_cloudrun_reader_hp_items_select
  ON research.research_job_items FOR SELECT TO clinic_cloudrun_reader
  USING (EXISTS (
    SELECT 1 FROM research.research_jobs j
    WHERE j.id = research_job_items.job_id AND j.kind = 'hp'
  ));
-- DBAレビュー・承認後の適用時だけ次を有効化: COMMIT;
-- 現在は実行しない。
```

ロールバック案も実行禁止です。適用前に不足権限だったことを確認し、この変更で作成したpolicyとgrantだけを戻します。

```sql
BEGIN;
DROP POLICY IF EXISTS clinic_cloudrun_reader_hp_items_select
  ON research.research_job_items;
DROP POLICY IF EXISTS clinic_cloudrun_reader_hp_jobs_select
  ON research.research_jobs;
REVOKE SELECT (job_id, clinic_id, state, result)
  ON research.research_job_items FROM clinic_cloudrun_reader;
REVOKE SELECT (id, kind, status, created_at)
  ON research.research_jobs FROM clinic_cloudrun_reader;
REVOKE USAGE ON SCHEMA research FROM clinic_cloudrun_reader;
-- DBA確認・承認後のrollback時だけ次を有効化: COMMIT;
-- 現在は実行しない。
```

## Reader受入検査

DBAが別途権限変更を承認・適用し、Reader専用DSNを外部の`reader.env`へ設定した後、Streamlitを起動せずに次の検査を実行します。コードはReaderでread-only transactionを開き、preflight、医院投影の可視件数、HP Job件数、HP Job item件数をSELECTだけで確認します。Job IDや個別医院情報は表示しません。

```sh
.venv/bin/python scripts/check_mac_hp_reader.py
```

検査が通った後の画面起動は`./scripts/launch_v2_mac_normal.sh --headless`です。Readerファイルがない、または権限/RLSが不足する場合は検査でfail-closedします。

既存Reader roleの監査はSupabase管理接続からSELECTのみで実施しました。これはMac Reader資格情報によるpreflight成功を意味しません。上記不足権限は未適用で、`reader.env`も未作成のためMac起動preflightは未完了です。本番DB変更は行っていません。
