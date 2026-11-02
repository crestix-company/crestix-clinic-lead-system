# メール自動調査（Email Research）アーキテクチャ調査

調査対象コミット: `8e9d2c3`。実行・DB書き込みは一切行わず、静的なコード読解のみで作成。

## 1. データ保存先（結論）

**メール自動調査は常にPostgreSQL（Supabase含む）専用であり、SQLiteは一切使用しない。**
医院マスター本体（`CLINIC_DATA_BACKEND`でsqlite/supabaseを選択可能）とは完全に独立した、別個の接続・別個のデータベースを要求する設計になっている。

- 接続先は環境変数 `EMAIL_RESEARCH_DB_URL`（`src/repository/email_database.py:16`）で指定するPostgreSQL接続文字列のみ。スキームが`postgres`/`postgresql`でない場合は拒否される（`email_database.py:48`付近の`_database_identity`）。
- 接続実装は`src.repository.supabase_adapter.connect`を流用（`email_database.py:186`）。すなわちSupabaseでも素のPostgreSQLでも、Postgresワイヤプロトコルで話せるエンドポイントなら動作する。
- **安全設計上の重要な制約**：`email_database.py:1-5`のモジュールdocstringに明記されている通り、「Clinic/HP runtime credentials are never an Email DB fallback.（医院マスターの接続情報はEmail DBのフォールバックに使われない）」。既定の`CROSS_DATABASE_MODE`（`email_database.py:21-34`）では、`EMAIL_RESEARCH_DB_URL`は医院マスターの`SUPABASE_RUNTIME_DB_URL`と**異なる接続先であることが要求される**（identityが同一だと拒否）。運用者が明示的に`EMAIL_RESEARCH_DB_MODE=same_database`を設定した場合のみ、同一DBの利用が許可される。
- テーブル定義は`src/repository/email_research_repository.py:27-29`：
  - `CANDIDATE_TABLE = "email_research.email_candidates"`
  - `STATUS_TABLE = "email_research.clinic_email_status"`
  - `AUTO_RUN_TABLE = "email_research.email_auto_runs"`
  - いずれも`email_research`という専用スキーマ配下。

**未解決点（断定しない）**：本セッションで`~/ClinicLeadMain`の資格情報を一時的に参照してSupabase本番DBを読み取り専用で確認した際、確認できたのは`public.clinic_email_enrichment`テーブルのみで、`email_research.*`スキーマは見つからなかった。これは上記の「医院DBとEmail DBは別エンドポイントである設計」と整合しており、`public.clinic_email_enrichment`は別プロジェクト（Email Research専用のPostgres/Supabase）ではなく医院マスター側プロジェクトのテーブルである可能性が高い。すなわち**Email Research専用DBの実体はこのセッションでは未確認**（`EMAIL_RESEARCH_DB_URL`の資格情報自体を保持していないため）。本番での実体確認は別途Email Research専用の資格情報が必要。

## 2. 呼び出し順序（起動 → 対象取得 → HP検索 → メール抽出 → 判定 → DB保存）

### 2-1. 起動（UI起点）
- `src/email_research_ui.py:121` `_auto_runner()` がランナーを構築：
  - `repository_factory`（`email_research_ui.py:123-126`）→ `build_email_repositories()`（`src/repository/email_database.py:174`）でDB接続を確立し`bundle.email_research`を返す
  - `target_reader`（`email_research_ui.py:131`）→ `_read_email_targets()`（`email_research_ui.py:134-138`）
  - `research_one` → `build_default_research_one()`（`src/master/email_auto_run.py:508`）
  - `config` → `load_email_research_config()`
- UIから`EmailAutoRunner.start(run_id=None)`（`src/master/email_auto_run.py:83`）が呼ばれ、`repo.sync_targets()`で対象を同期後、ワーカースレッドを起動（`run()` → `_worker()`）

### 2-2. 調査対象取得
- `_read_email_targets()`（`email_research_ui.py:134`）→ `connect_email_target_reader()`（`src/repository/email_target_reader.py`）→ `reader.eligible_targets()`：HP確認済み・公式URLあり・未調査のClinicを抽出
- ワーカーループ内では`repo.claim_next(worker_id=..., run_id=...)`（`email_auto_run.py:276`）でDB側のリース（排他制御）付きで1件ずつクレーム

### 2-3. HP検索 → メール抽出
- `research_one(claim, config)`（`email_auto_run.py:414`経由で呼ばれる、実体は`build_default_research_one.research_one`、`email_auto_run.py:510`）が
  `src.enrichment.email_research.formal_pipeline.research_clinic`（`formal_pipeline.py:152`）を呼ぶ
- `research_clinic`は「公式HP → NAVII → 条件付き最大2回のSearch API」の順で`run_pipeline`（`src/enrichment/email_research/pipeline.py:207`）を呼び出す：
  1. 公式HP crawl（`official_hp_email_crawler`経由、mailto/本文/お問い合わせページ/法人ページの順）
  2. 未確定ならWeb検索（`search_candidate_adapter`）で候補発見のみ（即verified扱いにしない）
  3. AI Overview（`ai_overview_adapter`）も候補発見のみ
  4. 5/6で見つかった候補は必ず`candidate_verifier.verify_candidate`で公式ページ再確認してからでないとVERIFIED_EMAILにしない（`pipeline.py`モジュールdocstリング、`formal_pipeline.py:9-16`のimportと整合）

### 2-4. 判定（本人確認・信頼性評価）
- `src.enrichment.email_research.clinic_identity.assess_email_ownership`：メールアドレスの名義（法人/個人/部門）が医院と一致するか判定
- `src.enrichment.email_research.official_site_trust.assess_official_trust`：候補ページが公式サイトとして信頼できるか判定
- `src.enrichment.email_research.email_role_classifier.classify_email_role`：代表/予約窓口/一般等の役割分類
- `src.master.email_sales_eligibility`：営業送付対象として適格かどうかの最終判定
- 最終ステータスは6種に正規化：`VERIFIED_EMAIL` / `SEARCH_CANDIDATE` / `FORM_ONLY` / `REVIEW` / `NO_EMAIL` / `FETCH_FAILED`（`pipeline.py`冒頭docstring）

### 2-5. DB保存
- ワーカーが結果を受け取った後、`repo.complete_claim(...)`（`email_auto_run.py:348`）を呼び、`email_research.email_candidates`・`email_research.clinic_email_status`テーブルへ書き込む
- 実行全体のメタデータ（開始/停止/一時停止/完了ステータス）は`repo.update_auto_run(run_id, status=...)`（`email_auto_run.py:132,143,151,220,229`）で`email_research.email_auto_runs`テーブルへ記録

## 3. 関連ファイル一覧（今回調査対象5件＋依存先）

| ファイル | 役割 |
|---|---|
| `src/email_research_ui.py` | Streamlit UI。ランナー起動・ステータス表示・一覧表示（`email_auto_research_card`, `email_research_page`等） |
| `src/master/email_auto_run.py` | `EmailAutoRunner`：ワーカースレッド管理・DBクレーム/完了処理のオーケストレーション |
| `src/enrichment/email_research/` | 実際の調査ロジック9モジュール（`formal_pipeline.py`が起点、`pipeline.py`が核、`*_adapter.py`/`*_classifier.py`/`*_trust.py`/`*_identity.py`が各判定ステップ） |
| `src/repository/email_research_repository.py` | PostgreSQL（Supabase含む）専用リポジトリ。`SupabaseEmailResearchRepository`クラス、claim/complete/update_auto_run等のSQL実装 |
| `src/repository/email_database.py` | 接続先決定・安全検証（医院DBとの混同防止、cross_database/same_databaseモード） |

## 4. 今回の制約事項の遵守

本調査はコード読解のみで完結し、メール調査ジョブの実行・DBへの接続/書き込みは一切行っていない。
