# テスト関連222件の精査結果と整理方針

対象コミット: `8e9d2c3`（`chore/mac-local-file-cleanup`）。調査・分類のみ、削除は未実施。

## 1. 調査範囲と方法

`tests/` 配下 222件（`test_*.py` 214件 + サポート/フィクスチャ9件。旧v1の`tests/test_app.py`は別カテゴリのため対象外）を、以下の方法で**ファイル名ではなく中身**を確認して分類した。

- 各`test_*.py`の`import`文を解析し、検証対象の実装ファイル（`app_v2.py` / `src/**` / `scripts/**` / `mhlw_dry_run/**`）を特定
- `app_v2.py`を文字列として直接読み込み、ソースコードのパターンをassertするテスト（import検出だけでは見つからない「Cloud Run限定ガードがMacのデフォルト動作に漏れていないか」を保護する一群）を個別に発見・保護
- `git grep`でテスト間参照・フィクスチャ参照の有無を確認
- `pyproject.toml`（`testpaths=["tests"]`）と`.github/workflows/test.yml`（`python -m pytest -q`）から、収集・CI実行の有無を確認（222件は全て収集・実行対象）

結果は `docs/file_cleanup_inventory/test_files_audit.csv`（テストコード223件）と `docs/file_cleanup_inventory/test_data_audit.csv`（リポジトリ全体の`.sqlite3/.csv/.json/.bak`等55件）に出力した。

## 2. 分類結果サマリ

| 判定 | 件数 | 内容 |
|---|---:|---|
| 維持推奨 | 206件（test_*.py） + 7件（サポート/フィクスチャ） = 213件 | Windows/Mac共通ロジック・ランチャー・Supabase・厚生局・Comdesk・Maps・HP調査・メール自動調査・マイナビ連携・Cloud Runガード保護テストを検証 |
| 削除候補 | 8件（test_*.py） + 2件（孤立フィクスチャ） = 10件 | 下記3节参照 |
| 個別調査 | 0件（test_*.py） | 当初15件を手動確認し全て「維持推奨」に確定（詳細は4節） |

**失われる個人情報・秘密情報のリスク**：削除候補10件・保留のサンプル/成果物ファイル群を確認した範囲では、実在の個人を特定できる情報（氏名・連絡先等の個人情報）は検出していない。医院（法人）の名称・所在地等の業務情報が一部に含まれる可能性があるのみで、ファイル内容の詳細はこの報告やCSVには転記していない。

## 3. 削除候補（安全にGitHubから削除可能と判断した10件）

### 3-1. Cloud Run専用テスト（8件、計65関数）

いずれも「①既に削除済みの旧コードのみを検証」ではなく「②Windows版では使用しないCloud Run専用処理のみを検証し、`app_v2.py`のテキストを一切検証しない」ことを個別確認済み（`app_v2`という文字列の出現回数がゼロ）。

| ファイル | 検証対象 | テスト関数数 |
|---|---|---:|
| `tests/test_cloud_run_comdesk_import_writer.py` | `src/cloud_run/comdesk_import_writer.py` | 5 |
| `tests/test_cloud_run_deployment_context.py` | `src/cloud_run/deployment_context.py` | 2 |
| `tests/test_cloud_run_feature_flags.py` | `src/cloud_run/feature_flags.py` | 3 |
| `tests/test_cloud_run_job_control.py` | `src/cloud_run/{dispatch_state,job_control,usage_budget}.py` | 29 |
| `tests/test_cloud_run_kouseikyoku_import_writer.py` | `src/cloud_run/kouseikyoku_import_writer.py` | 5 |
| `tests/test_cloud_run_maps_collection_writer.py` | `src/cloud_run/maps_collection_writer.py` | 5 |
| `tests/test_cloud_run_maps_import_writer.py` | `src/cloud_run/maps_import_writer.py` | 5 |
| `tests/test_cloud_run_operator_identity.py` | `src/cloud_run/operator_identity.py` | 11 |

**注意**：検証対象の`src/cloud_run/*`モジュール自体は`app_v2.py`がモジュールレベルでimportしているため**削除しない**（過去の整理で確定済み）。削除候補はあくまで「そのモジュールの単体テストのみ」であり、`app_v2.py`やMac/Windows共有パスを保護する価値はない。

### 3-2. 孤立フィクスチャ（2件）

| ファイル | 孤立理由 |
|---|---|
| `tests/fixtures/email_prefetch_v6_should_fetch.json` | リポジトリ全体でファイル名の参照がゼロ。兄弟フィクスチャ（`email_role_v6_human_audit.json`等）は現役だがこれのみ利用されていない |
| `tests/fixtures/cloudrun_readonly_projection_pg.sql` | 唯一の利用元`scripts/verify_cloudrun_readonly_projection_pg.py`を前回コミット（`8e9d2c3`）で既に削除済みのため孤立 |

### 3-3. テストデータ（訂正：該当なし）

**訂正**：当初`data/v254_full_run_state.json`・`data/v254_full_run_state_v2.json`を「唯一の利用元スクリプトが削除済みのため孤立」と分類していましたが、再確認の結果、`run_remaining_tokyo_hp_v254.py`／`run_remaining_tokyo_hp_v254_v2.py`は**現在もリポジトリに存在**し、両ファイルを`STATE`変数で直接参照していることを確認しました。したがってこの2件は孤立ではなく「保留」（スクリプト自体の要否が未確定のため）に訂正し、削除候補から除外します。`docs/file_cleanup_inventory/test_data_audit.csv`も訂正済みです。

## 4. 「個別調査」から「維持推奨」に確定した15件の内訳

当初import解析だけでは対象を特定できなかった15件を個別に開封確認した結果、全て現役の重要機能を検証していることが判明した。

| ファイル | 実際の検証対象 |
|---|---|
| `test_cloud_hp_email_ui_safety.py`, `test_cloud_run_comdesk_import_ui.py`, `test_cloud_run_kouseikyoku_import_ui.py`, `test_cloud_run_maps_collection_admin_ui.py`, `test_cloud_run_maps_import_ui.py`, `test_cloudrun_parity_menu_visibility.py`, `test_outreach_read_only_isolation.py` | `app_v2.py`を**文字列として直接読込み**、Cloud Run限定ガードがMac既定動作（`read_only`やめメニュー構成）に漏れていないことを保護。import文では検出できないパターン |
| `test_email_human_audit_replay.py` | `scripts/email_human_audit_replay.py`（メール調査Human Audit再生） |
| `test_email_target_reader_role_sql.py` | Email Target Reader用DBロール定義SQLのテキスト検証 |
| `test_launch_v2_env.py`, `test_mac_runtime_single_instance.py` | `scripts/launch_v2.py`（本セッションで実際に使用した公式ランチャーそのもの） |
| `test_mac_supabase_reader_preflight.py` | `scripts/launch_v2_mac_reader.py`（importlib動的ロード） |
| `test_outreach_app_test.py` | `streamlit.testing.v1.AppTest`で`app_v2.py`を実行（文字列リテラルpath指定のため静的import検出に出なかった） |
| `test_mhlw_deterministic_sidecar.py` | `mhlw_dry_run.finalize_mhlw_sidecar`（import検出の対象prefixに`mhlw_dry_run.`が未登録だったための見落とし） |
| `test_rule_c_identity.py` | `mhlw_dry_run.rule_c_identity_rules` |

**教訓**：「Cloud Runという名前のテストでも、`app_v2.py`のテキストや共有ロジックを検証している場合は維持する」という本依頼③の方針どおり、ファイル名だけでの判定は8件を除いて全て不適切だった。

## 5. 必ず維持するテスト（③で指定された重要機能）の確認結果

| 重要機能 | 対応テスト（代表例） | 維持確認 |
|---|---|---|
| Windows BAT・PowerShell起動 | `test_launch_v2_env.py`, `test_mac_runtime_single_instance.py`, `test_mac_supabase_reader_preflight.py` | ✅ |
| Supabase・PostgreSQL接続 | `test_cloud_run_config.py`（編集済み・現役部分）, `test_email_target_reader_role_sql.py`, Supabase repository系多数 | ✅ |
| 医院マスターのインポート | `src.master.*`を検証する多数のテスト | ✅ |
| Comdesk UUID保持・重複防止・CSV出力 | `test_comdesk_*.py`群 | ✅ |
| Google Maps取得・医院紐づけ | `test_national_maps_*.py`, `test_*google_maps*.py` | ✅ |
| HP調査・HPランク | `src.enrichment.*`を検証するテスト群 | ✅ |
| 営業対象フィルタリング | `test_marketing_filters.py`, `test_sales_classification*.py`等 | ✅ |
| メールアドレス自動調査 | `test_email_research_*.py`等、`src.enrichment.email_research`検証群 | ✅ |
| マイナビ記事URL・医院詳細URL・PR表記分類 | `src.outreach.*`を検証するテスト群 | ✅ |

全て維持推奨に分類されており、削除候補10件には一切含まれていない。

## 6. GitHub保持 vs Windows配布対象からの除外（A案 / B案比較）

| 観点 | A案：GitHubから削除 | B案：GitHubに保持し配布対象のみ除外 |
|---|---|---|
| ファイル数への影響 | 削除候補10件（+将来の不要テスト追加時も都度削除判断が必要） | リポジトリのファイル数は減らないが、Windows配布物（`requirements-lock.txt`・起動チェーンに同梱されるもの）には元々`tests/`は含まれていないため**現状でも実害なし** |
| リスク | 削除候補8件は`app_v2.py`非依存と個別確認済みで低リスクだが、将来`src/cloud_run/*`の仕様変更時に単体テストが失われ回帰検知が弱まる | リスクなし。pytest・CIでの回帰保護を維持したまま、配布物への混入もそもそも発生していない |
| 保守性 | リポジトリがわずかに軽量化されるが、Cloud Run側の開発者（別エンジニア）が将来このテストを再度必要とした場合、Git履歴から復元する手間が生じる | 現状維持で追加コストゼロ。Cloud Run担当エンジニアの開発体験を損なわない |
| 実際の配布経路の確認結果 | — | Windowsの起動チェーン（`ClinicLead.bat`→`scripts/update_and_launch_windows.ps1`→`scripts/launch_v2_windows.ps1`）は`streamlit run app_v2.py`を実行するのみで、`tests/`ディレクトリを一切参照・同梱していない。`setup_v2_windows.bat`が導入する`requirements-lock.txt`にも`tests/`は含まれない。**つまりtests/配下のファイルは現状でも「Windowsの本番利用」に一切影響していない** |

### 推奨：**B案（現状維持）を基本としつつ、個別確認済みの3-1〜3-3の10件のみA案で削除**

理由：
1. `tests/`配下のファイルはそもそもWindows配布物に含まれておらず、「Windows本番利用に不要なだけ」という問題は実質的に発生していない（起動チェーンの検証で確認済み）。したがって「配布対象から除外する」作業自体が不要。
2. 213件（維持推奨）は現役機能の回帰保護であり、削除するメリットがリスクを上回らない。
3. 10件（8件のCloud Run専用単体テスト＋孤立フィクスチャ2件）は、`src/cloud_run/*`本体を維持する一方でその単体テストだけを削除しても、Mac/Windows側の回帰保護には一切影響しない。Cloud Run担当エンジニアが同テストを必要とする場合はGit履歴（本ブランチの前提コミット）から復元可能。

## 7. 集計

1. 調査したテストファイル総数：**222件**（`test_*.py` 214 + サポート/フィクスチャ9 - 旧v1に分類済みの1を除く。別途テストデータ55件も調査）
2. GitHubから安全に削除可能な件数：**10件**（テストコード8 + 孤立フィクスチャ2）
3. 維持推奨件数：213件（テストコード206 + サポート/フィクスチャ7）
4. 追加調査が必要な件数：0件（テストコードは全件確定）。テストデータ側で21件程度（`national_maps_output`配下の日付付き成果物12件、`samples/outputs/*`3件、`judged_all.xlsx`、`comdesk_template_28.csv`、`hp_test_5_integrated.csv`、`data/v254_full_run_state*.json`2件）
5. テストデータ・ログの削除候補件数：**0件**（当初`data/v254_full_run_state.json`/`_v2.json`を孤立と誤記載したが、利用元スクリプトが現存するため訂正し保留扱いとした。3-3節参照）
6. 削除によって失われるテストケース数：**65関数**（Cloud Run専用8ファイル合計。孤立フィクスチャ2件はテスト関数を持たないため0）
7. Windows本番運用への影響：**ゼロ**（起動チェーンは`tests/`を一切参照しないことを確認済み。削除候補10件もapp_v2.py非依存と個別確認済み）
8. 推奨する整理方針：B案ベース（tests/配下は原則GitHubに保持）＋個別確認済みの12件のみ別途の削除作業で対応（本調査では未実施）
