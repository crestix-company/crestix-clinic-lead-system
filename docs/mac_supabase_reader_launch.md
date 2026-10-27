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

この作成例は既存ファイルを上書きしません。各ファイルに権限者から取得した値を安全なエディタで記入してください。`reader.env`と`preview-reader.env`は各1行の`SUPABASE_READONLY_DB_URL=...`のみです。`runtime-writer.env`はHP通常版専用で、`SUPABASE_RUNTIME_DB_URL=...`のみです。`hp-provider.env`は`TAVILY_API_KEY=...`のみを保存します。ReaderとWriterは別ロール・別URLでなければなりません。パスワード、接続URL、APIキーをコマンド引数、シェル履歴、Git管理ファイルへ置かないでください。起動スクリプトは秘密値を表示しません。

## Readerの最小参照範囲

READ ONLY閲覧画面は投影relationを使用します。Comdesk Previewは別ReaderとPreview固有relationを使用します。HP通常版はHP対象検索・結果・Job進捗のため、次のSELECTが必要です。HP通常版はEmail候補・Email状態を読みません。

- `public.clinic_cloudrun_readonly` — 医院ID、医院名、住所、都道府県、診療科、Maps公式URLなど対象検索に必要な投影列
- `hp_research.clinic_hp_research` — HP調査状態・結果の参照
- `research.research_jobs`、`research.research_job_items` — Job状態と進捗
- `app_config.settings` — HP Job設定の参照

閲覧・営業一覧の利用時は、その画面の投影relationとして`public.clinic_email_enrichment`、`provenance.clinic_cloudrun_comdesk_rows`、`provenance.templates`、`treatment.clinic_treatment_research`、`treatment.clinic_research_status`も必要です。Email候補本文・候補行を読む`email_research.email_candidates`への直接権限は不要です。メール候補の画面・調査はHP通常版の対象外です。

DB管理者は上記relationの列投影、スキーマUSAGE、SELECT、RLSを確認してください。医院マスター全件を許可するかは利用承認範囲に従い、無条件の全国開示を暗黙に設定しないでください。基表RLSを使う場合は、専用Readerに対するSELECT policyで承認済み医院範囲を指定します。投影Viewを使う場合は、所有者権限でRLSを迂回しない構成か（例：適切な`security_invoker`と基表policy）をDBAが検証してください。これは権限設計案であり、今回DDL/GRANT/Policy作成は行いません。

起動時のpreflightは接続roleの管理属性、書込・シーケンス権限、必要なSELECT、医院行の可視性を検査します。権限が不足した場合は停止します。`clinic_runtime`は書込権限を持つためReaderとして使えません。実データ件数の期待値は環境・RLSに依存します。PRレビューで示された164201件は受入確認時の参照値であり、コードの固定条件ではありません。

## HP通常版の安全境界

起動だけではJob作成・Worker開始をしません。対象検索・件数・候補プレビューと進捗はReader経由です。Job登録とWorker開始は別の明示操作で、HP Writer経路だけを使います。Auto Run、Maps取込、Email調査、Comdesk反映、Gmail操作は通常HP画面から呼び出しません。既存PAUSED Jobを自動で再開・変更しません。実DBでのJob登録・Worker開始には別途承認が必要です。

## DB管理者レビュー用SQL案（実行禁止）

下記は権限案の説明用であり、実行してはいけません。最小のSELECTとRLSの実際の適用範囲は、現在のView定義・基表RLS・承認範囲を確認してDB管理者が決定してください。

```sql
-- 例示のみ。実行禁止。必要なView/tableに限って個別に付与する。
GRANT USAGE ON SCHEMA public, hp_research, research, app_config TO <dedicated_reader>;
GRANT SELECT ON public.clinic_cloudrun_readonly,
  hp_research.clinic_hp_research,
  research.research_jobs, research.research_job_items,
  app_config.settings TO <dedicated_reader>;
-- 基表RLSが有効な場合は、承認済み医院範囲のSELECT policyを別途設計する。
```

現時点では外部Reader/Writer設定の準備状況を値を読まずに確認するまで、Mac起動可能とは扱いません。本番DB変更は行っていません。
