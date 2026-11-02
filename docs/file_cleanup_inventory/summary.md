# Clinic Lead System ファイル整理ブランチ — 残存ファイル棚卸し

- 対象ブランチ: `chore/mac-local-file-cleanup`
- 対象コミット: `8e9d2c3`（直前2コミットで179ファイルを削除済み）
- 棚卸し対象: `git ls-files` で取得した追跡対象ファイル **741件**
- 調査方法: ディレクトリ・ファイル名規則によるカテゴリ分類 + `git grep -F` によるファイル名／モジュールパスの静的参照件数カウント + 本セッションで個別に深掘り済みの既知ファイル（Windows起動チェーン9件、かんたん操作/営業対象出力/メール自動調査/マイナビ連携の中核ファイル、DBバックアップ、壊れたシンボリックリンク2件等）の個別判定を上書き適用

## 機能分類別件数

| 分類 | 件数 |
|---|---:|
| テスト | 222 |
| Google Maps・HP調査 | 79 |
| 医院マスター・Comdesk | 69 |
| その他（未分類／scripts配下の個別検証が必要なもの中心） | 138 |
| ドキュメント | 55 |
| Supabase・PostgreSQL | 41 |
| データ・サンプル | 32 |
| マイナビ記事連携 | 32 |
| メール自動調査 | 24 |
| Streamlit UI | 15 |
| Cloud Run共通依存 | 14 |
| Windows起動 | 8 |
| リポジトリ設定 | 9 |
| 旧v1 | 3 |
| **合計** | **741** |

## 判定別件数

| 判定 | 件数 | 内訳 |
|---|---:|---|
| 必須 | 290 | テスト222 + Google Maps・HP調査/医院マスター/メール調査/マイナビ連携/Streamlit UIの中核実装 + Cloud Run共通依存14（app_v2.py依存） + Windows起動8 + リポジトリ設定9 + Supabase migration本体 + data/配下のDB本体・バックアップ + 個別確認済みの主要ファイル |
| 保留 | 451 | 旧v1 3件、過去セッションで保留指定した5件（update_midday_procedure_signal.py等）、ドキュメント55件の大半、scripts配下の未個別検証ファイル、national_maps_output/national_maps_staging/mhlw_dry_run配下の日付付き成果物、samples/artifacts/assets配下 など |

## 削除候補の分類

「安全に削除可能」「追加調査が必要」の2区分で評価した結果：

- **安全に削除可能：2件**
  - `.agents/skills/developing-with-streamlit`
  - `.claude/skills/developing-with-streamlit`
  - いずれも `.venv_mac/lib/python3.12/site-packages/streamlit/.agents/skills/developing-with-streamlit` を指す**壊れたシンボリックリンク**（このworktreeに`.venv_mac`が存在しないため）。ローカル仮想環境に依存する自動生成物で、コード・テスト・ドキュメントからの参照はゼロ。Gitで追跡されたシンボリックリンクそのものを消しても`.venv_mac`（gitignore対象）には影響しない。**ただし今回は調査のみとし、削除は実施していません。**

- **追加調査が必要：449件**
  - このブランチで確実に「不要」と断定できるファイルは前2回のコミットで既に削除済みのため、今回の機械的棚卸しで新たに「安全に削除可能」と断定できたのは上記2件の壊れたシンボリックリンクのみです。
  - 残り449件は、以下のいずれかに該当するため個別の人間/追加調査が必要と判定しました（一律「不要」と断定していません）。
    - ドキュメント55件の大半：直接のコード参照が無いのは通常の状態であり、内容が陳腐化しているか現役の設計文書かは個別の内容確認が必要
    - `scripts/`配下の多数（「その他」138件の中心）：Windows起動チェーン・Supabase migration関連以外は、CLIから直接実行される運用スクリプトの可能性があり、静的grepの参照ゼロ＝未使用と断定できない
    - `national_maps_output/`・`national_maps_staging/`・`mhlw_dry_run/`配下の日付付き成果物：過去の調査で「現役パイプラインの一部」と報告されているが、個々の日付付きディレクトリが一回限りの出力か否かは未検証
    - `samples/`・`artifacts/`・`assets/`：テスト・UIからの参照有無を個別確認していない
    - `update_midday_procedure_signal.py`等、前回セッションで明示的に保留指定した5件：理由は`deletion_candidates.csv`に個別記載

## 今回の作業範囲

- 調査・一覧作成のみを実施し、**ファイルの削除・変更は一切行っていません**
- 出力ファイル：
  - `docs/file_cleanup_inventory/remaining_files.csv`（741件全件、7列）
  - `docs/file_cleanup_inventory/deletion_candidates.csv`（保留・削除候補451件、5列）
  - `docs/file_cleanup_inventory/summary.md`（本ファイル）
