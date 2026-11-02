# 追加整理の推奨事項（今回の精査結果）

対象コミット: `8e9d2c3`。本ドキュメントは調査結果のみであり、記載したファイルの削除は実施していない。

## 1. 今回新たに「安全に削除可能」と個別確認できたファイル：13件

これまでの2回のコミット（179ファイル削除）に続き、今回の精査で以下13件を個別に確認し、削除してもMac/Windows版の機能・CIに影響しないと判断した。

### 1-1. 壊れたシンボリックリンク（2件）
```
.agents/skills/developing-with-streamlit
.claude/skills/developing-with-streamlit
```
リンク先`.venv_mac/lib/python3.12/site-packages/streamlit/.agents/skills/developing-with-streamlit`がこのworktreeに存在しない（ローカル仮想環境に依存する自動生成物）。リポジトリ全体で参照ゼロ。

### 1-2. Cloud Run専用の単体テスト（8件、計65テスト関数）
```
tests/test_cloud_run_comdesk_import_writer.py
tests/test_cloud_run_deployment_context.py
tests/test_cloud_run_feature_flags.py
tests/test_cloud_run_job_control.py
tests/test_cloud_run_kouseikyoku_import_writer.py
tests/test_cloud_run_maps_collection_writer.py
tests/test_cloud_run_maps_import_writer.py
tests/test_cloud_run_operator_identity.py
```
`app_v2.py`の文字列・import・共有ロジックへの依存がゼロ（`app_v2`という文字列の出現回数を個別確認）であることを確認済み。検証対象の`src/cloud_run/*`モジュール本体はapp_v2.pyが依存するため維持するが、その単体テストのみは削除可能。

### 1-3. 孤立したテストフィクスチャ（2件）
```
tests/fixtures/email_prefetch_v6_should_fetch.json
tests/fixtures/cloudrun_readonly_projection_pg.sql
```
前者はリポジトリ全体で参照ゼロ（兄弟フィクスチャは現役）。後者は唯一の利用元`scripts/verify_cloudrun_readonly_projection_pg.py`を前回コミットで既に削除済みのため孤立。

### 1-4. 強い削除候補（1件、個別の内容確認は未実施のため最終判断は要人手確認）
```
src/utils/logging_utils.py
```
リポジトリ全体で`logging_utils`という文字列の参照がゼロ（import・文字列言及とも）。純粋なライブラリモジュールでCLIからの直接実行経路も考えにくい。ただし内容未確認のため、本リストでは「強い削除候補」として提示するに留め、削除可否の最終確認は別途人手で行うことを推奨。

**合計：13件**（うち12件は参照ゼロを個別確認済みで高信頼度、1件は要最終確認）

## 2. 当初「孤立」と誤判定し訂正した項目

`data/v254_full_run_state.json` / `data/v254_full_run_state_v2.json` の2件は、当初「唯一の利用元スクリプトが削除済み」と誤って分類しましたが、再確認の結果`run_remaining_tokyo_hp_v254.py` / `run_remaining_tokyo_hp_v254_v2.py`は**現存**しており、`STATE`変数でこの2ファイルを直接参照していることを確認しました。したがって削除候補から除外し、「保留」（スクリプト自体の要否が未確定）に分類し直しています（`test_data_audit.csv`参照）。

## 3. 「その他」138件・「ドキュメント」55件・「旧v1」3件の再精査結果サマリ

| カテゴリ | 維持確定 | 削除可能 | 追加確認必要 |
|---|---:|---:|---:|
| その他（138件） | — | 2 | — |
| ドキュメント（55件） | — | 0 | — |
| 旧v1（3件） | — | 0 | 3（全件） |
| **3カテゴリ合計196件** | **109件** | **2件** | **85件** |

詳細は `docs/file_cleanup_inventory/retriage_451_result.csv` を参照。85件の「追加確認必要」は、以下の理由により今回は断定を避けた：

- `scripts/`配下の多数（phase番号付き監査・移行スクリプト等）：静的参照はゼロだが、CLIから手動実行される運用ツールの可能性を排除できない（本依頼で明示された注意点「静的参照が0件でも、直接実行されるスクリプトは未使用とは判定しない」を遵守）
- `docs/`配下の多数（特に`docs/supabase_migration/`の個別ステージ文書）：各移行ステージの実施記録であり、内容が完全に陳腐化しているか将来の参照価値があるかは個別の内容精査が必要
- `generate_treatment_human_review.py`, `hp_test_5_integrated.csv`, `test_midday_real_clinic.py`, `update_midday_procedure_signal.py`：前回セッションで指摘した保留理由（本番DBパス直結、IDE作業中の形跡、構成不一致）が未解消
- `rerun_latest_hp_job_v251/v252.py`, `run_remaining_tokyo_hp_v254(_v2).py`：本番相当のSQLite DBへ書き込む実行可能スクリプトであり、過去にプロンプトインジェクション攻撃の標的になった経緯があるため特に慎重な個別確認が必要
- `app.py` / `start_windows.bat` / `tests/test_app.py`（旧v1 3件）：他PC・Windows環境での利用可能性を排除できないまま

## 4. メール自動調査・マイナビ記事連携の調査からの追加知見（削除判断への影響）

- メール自動調査関連5ファイル（`src/email_research_ui.py`等）・マイナビ記事連携関連ファイル（`src/outreach/*`等）は、いずれも今回の削除候補・追加確認リストに含まれていない（全て必須/維持確定）。アーキテクチャ調査により、これらが現役の重要機能であることが改めて裏付けられた。
- `src/scoring/research_scoring.py`のMEDIA辞書（マイナビ記事掲載シグナル検出）は、マイナビ記事連携機能とは別系統の一般HP調査ロジックの一部であり、誤って「マイナビ連携機能」の一部として削除対象にしないよう注意が必要（`mynavi_article_architecture.md`4節参照）。

## 5. 今回の集計

- 新たに「安全に削除可能」と判定：**13件**（うち12件高信頼度、1件要最終確認）
- 「追加確認必要」として保留：**85件**（その他/ドキュメント/旧v1）＋**21件程度**（テストデータ、`test_data_audit.csv`参照）
- 削除は今回実施していない。削除する場合は別途の作業として、本ドキュメント1節の13件から着手することを推奨する。

## 6. 第3段階整理での実施結果（追記）

上記1節の13件のうち12件（`src/utils/logging_utils.py`を除く）を削除実行した。削除前の最終確認で、`tests/test_cloud_run_job_control.py`は`tests/test_cloud_hp_executor.py`が`FakeSettingsConnection`クラスをimportする依存先であることが判明し、削除するとpytest収集エラーになるため**削除を取りやめ復元**した。

- 実際に削除：11件（`.agents/`・`.claude/`の壊れたシンボリックリンク2件、Cloud Run専用単体テスト7件＝36関数、孤立フィクスチャ2件）
- 削除を取りやめ復元：1件（`tests/test_cloud_run_job_control.py`）
- 保留継続：`src/utils/logging_utils.py`（今回の削除対象に含まれず）

削除後のpytest結果：2444 passed, 57 skipped, 2 failed（既知の2件のみ、新規失敗なし）。
