# データ保存先マップ（主要機能別）

`email_research_architecture.md`・`mynavi_article_architecture.md`の調査結果と、過去のセッションでの検証結果を統合した一覧。コード読解および本セッション内のREAD ONLY接続確認のみに基づく。書き込み・Migrationは一切実施していない。

| 機能 | 保存先 | 選択方法 | 根拠 |
|---|---|---|---|
| 医院マスター（かんたん操作・営業対象・出力） | SQLite（ローカル） または PostgreSQL/Supabase | 環境変数`CLINIC_DATA_BACKEND`（sqlite/supabase）。`config/production_runtime.env`が既定値`supabase`を設定、ローカル資格情報（`.supabase-runtime.env.local`）が無い場合は明示的エラーで起動拒否（fail-closed） | `app_v2.py`, `scripts/launch_v2.py:load_runtime_env`。本セッションの起動検証で`data/clinics_before_v21_20260915_211529.sqlite3`（LFS管理）への実接続を確認 |
| メール自動調査（Email調査） | PostgreSQL専用（Supabaseでも可だが医院マスターとは**別エンドポイント必須**） | 環境変数`EMAIL_RESEARCH_DB_URL`。既定の`CROSS_DATABASE_MODE`では医院マスターと異なる接続先を強制（`same_database`モードを明示的に設定しない限り） | `src/repository/email_database.py:1-5,16,21-34`。SQLiteは選択肢として存在しない |
| マイナビ記事連携（メール送付文章） | **DBなし。CSVファイルの読み込みのみ** | 環境変数`OUTREACH_ARTICLE_MASTER_CSV`・`OUTREACH_ARTICLE_JOIN_CSV`、または画面アップロード | `src/outreach_ui.py:38-57`, `src/outreach/article_adapter.py` |
| Comdesk HOLD確認・UUID照合プレビュー（隔離レビュー） | 専用SQLite（`CLINIC_COMDESK_REVIEW_DB`で指定） | 環境変数。医院マスター本体とは別ファイル | `app_v2.py:2918-2926`、`src/comdesk_hold_review_ui.py`, `src/comdesk_uuid_preview_ui.py` |
| 厚生局データ統合（mhlw_dry_run） | SQLiteサイドカー（`finalize_mhlw_sidecar.py`が構築） | ローカルファイル生成 | `mhlw_dry_run/finalize_mhlw_sidecar.py` |
| Cloud Run（参考・Windows版では不使用） | PostgreSQL/Supabase（読み取り専用ロール） | `src/cloud_run/*`、Cloud Run環境変数 | 既存調査（本棚卸し対象外、`app_v2.py`が依存のため維持中） |

## 本番Supabase接続確認の記録（本セッション内、READ ONLYのみ）

過去のセッションで`~/ClinicLeadMain`の資格情報を**コピー・保存せず**一時的に環境変数として参照し、明示的な`BEGIN TRANSACTION READ ONLY`を確認した上でSELECTのみ実行した結果：

- `current_user = clinic_runtime`
- `public.clinics` 件数：164,201件
- 読み取り可能テーブル26件：`app_config.settings`, `hp_research.clinic_hp_research`, `provenance.*`（8件）, `public.clinic_email_enrichment`, `public.clinics`, `research.*`（5件）, `treatment.*`（2件）, `extensions.pg_stat_statements*`
- **`email_research.*`スキーマは確認できなかった**（上記の「Email Research専用DB」節参照。別エンドポイントの可能性が高く、今回は資格情報を保持していないため未確認のまま）

この接続確認は今回のメール自動調査・マイナビ連携アーキテクチャ調査よりも前のセッションで実施済みのものを転記しており、本タスクで新たに本番接続は行っていない。

## 未解決・要確認事項

1. `EMAIL_RESEARCH_DB_URL`が指す実際のPostgreSQLエンドポイントの実体（別プロジェクトか、同一プロジェクトの別ロールか）は、該当の資格情報を持たないため本調査では確認できていない。
2. `data/clinics.sqlite3.before-maps-v4.bak`等のバックアップがLFS管理である点（`.gitattributes`: `*.bak filter=lfs`）は、`git`履歴上の軽量化効果がバックアップ削除では得にくいことを意味する（LFSオブジェクトは別ストレージ）。削除判断への影響はないが、サイズ見積もりの際に留意。
