# P2 書込アーキテクチャ現状報告（新規コードを追加しなかった理由）

Issue #126のP2は「取込/Maps/HP自動調査をブラウザで使うための安全な実装準備」を
求めているが、調査の結果、要求されている安全機構の大半は**すでに実装済み**だった。
重複した「見せかけのscaffolding」を追加するより、現状を正確に記録する方が有用と判断した。

## 既に実装済みの安全機構（新規追加なし）

| 要求 | 実装 | ファイル |
|---|---|---|
| Writer/Job専用の最小DBロール分離、fail-closed（暗黙フォールバック禁止） | 実装済み | `src/cloud_run/job_control.py`（`SUPABASE_JOB_CONTROL_DB_URL`必須、未設定なら例外） |
| 二重実行防止（同一Job IDの並行実行拒否） | 実装済み | `scripts/cloud_hp_worker.py`（`pg_try_advisory_lock(hashtext(job_id))`） |
| 二重クリック防止（予算の事前予約/解放） | 実装済み | `src/cloud_run/hp_executor.py`（`reserve_dispatch_usage`/`release_dispatch_usage`） |
| 単発Job・tasks=1・最大件数制限 | 実装済み | `src/cloud_run/hp_executor.py`、`precheck_budget()` |
| 既定OFFのFeature Flag群 | 実装済み | `CLINIC_COMDESK_SNAPSHOT_DIFF_ENABLED`, `OUTREACH_GMAIL_DRAFTS_ENABLED`, `EMAIL_AUTO_RUN_ENABLED`, `CLINIC_SHADOW_READ_ENABLED`（すべて既定false/unset） |
| 取込前プレビュー・医院情報非表示・UUID不一致時はHOLD | 実装済み | `src/master/comdesk_snapshot_import.py`, `render_comdesk_snapshot_diff_import()` |
| 開発DB限定・ログインロール検証（`clinic_runtime`以外は拒否） | 実装済み | `comdesk_snapshot_import.py::_ensure_development_import_enabled` |

## 未着手・新規コードを追加しなかった理由

- **Google Maps収集/取込の別ワークフロー化** — PR #92が既に対応済み（MERGEABLE/CLEAN、CI pass）。
  新規実装すると同じ機能のPRが2つ並び、レビュー負荷が増えるだけなので追加しなかった。
  次アクションはPR #92のレビュー・マージ判断。
- **Comdesk重複医院の名寄せプランナー** — PR #110が対応中（CI fail、要調査）。新規実装は重複になる。
- **厚生局マスターのCloud Run経由差分取込** — 対応するPRが存在せず、本当の空白。
  ただし「ブラウザから厚生局マスターを書き込める」機能自体が本番Writerロール・専用SAの
  提供を前提とするため、設計だけ先に進めても実際に有効化できない。今回は本番Secret/IAM
  変更が不可のため、コードを書いても実行確認ができず、未着手のまま明示する。
- **GAS/CRM連携のREAD ONLY確認画面** — PR #99が対応中だがCONFLICTING。本依頼の中心スコープ
  （メニュー/全国検索/Comdesk出力）の外側の機能であり、「無関係なPRを混ぜない」の方針に従い
  今回のP1/P2作業には含めなかった。

## 結論

P2で要求されている安全設計（最小権限ロール分離、fail-closed、二重実行防止、既定OFF、
事前プレビュー）は、既存コードベースに一貫したパターンとして既に存在する。今回の依頼で
新たに書くべきP2コードは見つからなかった。残っているのは（a）既存のオープンPR
（#92, #99, #101, #110）のレビュー・CI修正・マージ判断、（b）実際にWriter/Job用の
専用GCP SA・DBロールを人間が提供すること（本依頼では未承認・未実施）の2点のみ。
