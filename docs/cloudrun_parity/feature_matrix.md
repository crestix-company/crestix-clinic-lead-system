# Cloud Run ⇄ ローカル版 機能同等化 Feature Matrix

Issue #126 のP0要求（現状機能比較表）。基準: `origin/main` = `4e973b8293c6999b0eebd2a9c6d48f5eda401f28`（2026-10-20時点）。
分類: **A=使えるREAD ONLY** / **B=安全な表示だけ** / **C=WRITEのため未実装** / **D=今回対象外**。

| # | 機能 | 分類 | 状態 | 根拠（ファイル/PR） |
|---|---|---|---|---|
| 1 | 全国医院検索（都道府県/医科歯科/HPランク/院名・電話） | A | 実装済み | `src/repository/supabase_adapter.py` が `read_only_ui=True` で `public.clinic_cloudrun_readonly` を使用（PR #118, #121, #122） |
| 2 | 医院一覧の件数=一覧=出力件数の一致 | A | 実装済み | `listing()` が `count_read_only_ui`/`query_read_only_ui` を使用（app_v2.py:738） |
| 3 | 承認済み列のみ投影（院長名等7列は空欄） | A | 実装済み | `src/repository/runtime_store.py`（READ_ONLY除外列） |
| 4 | Comdesk CSV/XLSX 30列固定出力 | A | 実装済み | `src/master/comdesk.py`（`COMDESK_EXPORT_HEADERS` = 28+2） |
| 5 | 既存UUIDあり / 全件 / 除外後 の母集団区別表示 | B | 新規実装 | PR #127（`help=`テキスト） |
| 6 | 既存営業リスト（legacy cohort）とUUIDありの違いの明示 | B | 新規実装 | PR #127（`help=`テキスト）／定義自体は `src/master/scope.py` (`SCOPE_LEGACY_PRE_NATIONAL`) に既存 |
| 7 | ローカル版5メニュー構成の視認性（READ ONLYで名称・順序が分かる） | B | 新規実装 | PR #127（静的`st.caption`/`st.expander`、クリック可能ウィジェットは追加せず） |
| 8 | かんたん操作のStep1-4順序の視認性 | B | 新規実装 | PR #127 |
| 9 | Gmail下書き作成のREAD ONLY無効化 | B | 実装済み | PR #120、`OUTREACH_GMAIL_DRAFTS_ENABLED`既定false |
| 10 | HP Job制御のREAD ONLYからの到達不能性 | B | 実装済み（既存テストでピン留め） | `tests/test_cloud_run_read_only_isolation.py` |
| 11 | Cloud Run Web READ ONLYランタイム設定 | A | 実装済み | PR #104 (Phase A) |
| 12 | Cloud Run Jobs HP Worker（別実行体） | C（Workerは書込だが分離済み） | 実装済み・既定ではWeb非経路 | PR #105 (Phase B)、`scripts/cloud_hp_worker.py` |
| 13 | HP調査単発ディスパッチ（最大50件・Cloud Run） | C | コード実装済み・実行は未承認 | PR #106 (Phase C)、`src/cloud_run/hp_executor.py` |
| 14 | Job制御専用DBロール分離（fail-closed） | C | 実装済み・ロール未提供のため未使用可能 | `src/cloud_run/job_control.py` |
| 15 | 二重実行防止（advisory lock）・二重クリック防止（budget reserve） | C | 実装済み | `scripts/cloud_hp_worker.py`, `src/cloud_run/hp_executor.py` |
| 16 | Comdesk 2ファイル差分取込（プレビュー・UUID照合・HOLD） | C | 実装済み・既定OFF・開発DB限定 | PR #116、`CLINIC_COMDESK_SNAPSHOT_DIFF_ENABLED`（既定false） |
| 17 | Google Maps結果の収集・取込を別ワークフロー化 | C | PR作成済み・未マージ | PR #92（MERGEABLE/CLEAN、CI pass） |
| 18 | HP Auto Research オンデマンド設計ドキュメント | D | PR作成済み・CI失敗・未マージ | PR #101（CI fail） |
| 19 | Comdesk重複医院の安全な名寄せプランナー | C | PR作成済み・CI失敗・未マージ | PR #110（CI fail） |
| 20 | GAS/CRM連携のREAD ONLY確認・重複事前チェック | D（本依頼の対象外領域） | PR作成済み・main非衝突解消要 | PR #99（CONFLICTING） |
| 21 | 厚生局マスター取込・差分 | C | ローカル既存機能のまま（`import_master`）、Cloud Run READ ONLYでは到達不能（simple_workflow_ui早期return） | app_v2.py |
| 22 | Email調査・メール送付文章・詳細設定のUI位置+無効説明 | B | 実装済み（非表示方式を維持）＋新規の理由表示 | PR #127（sidebar解説）、既存のNAV[:2]ガード |

Full pytest: 2211 passed / 55 skipped（PR #127 ブランチで実行、regressionなし）。
