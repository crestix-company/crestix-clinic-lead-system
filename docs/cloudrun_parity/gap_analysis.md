# Gap Analysis — Issue #126 (Cloud Run機能同等化)

## 要約

着手前に `origin/main` を再取得したところ、ローカル `~/ClinicLeadMain` は112コミット遅れており、
Issue #126のP0/P1/P2チェックリストが対象とする機能の大半は、すでにPR #104-#122で
mainにマージ済みだった（詳細はPR履歴・`feature_matrix.md`参照）。本ドキュメントは
「何が既に終わっているか」と「何が本当に残っているか」を区別するために作成した。

## 既にmainで解決済み（再実装しなかった）

- Reader用の制限付きVIEW経由アクセス（`public.clinic_cloudrun_readonly` 等）への切替 — PR #118/#121/#122。
- Comdesk/医院投影の列制限・30列固定エクスポート — PR #121/#122、`src/master/comdesk.py`。
- CRM参照のREAD ONLY分離、Gmail下書きのREAD ONLY無効化 — PR #119/#120。
- Cloud Run Web READ ONLYランタイム、Jobs HP Worker分離、単発ディスパッチ — PR #104/#105/#106。
- Job制御専用DBロールのfail-closed設計、advisory lockによる二重実行防止、budget reserveによる二重クリック防止 — `src/cloud_run/job_control.py`、`scripts/cloud_hp_worker.py`、`src/cloud_run/hp_executor.py`（いずれも既存コード、新規追加なし）。
- Comdesk 2ファイル差分取込の既定OFF・開発DB限定プレビューフロー — PR #116。

これらに対して新しいコードを書くと、既存実装と重複するか、既存のpinned testと矛盾するため、
今回は**手を加えなかった**。

## 本当に残っていたP1ギャップ（今回実装・PR #127）

`app_v2.py`の`nav_options = NAV[:2] if read_only else NAV`と
`simple_workflow_ui(read_only=True)`の早期`return`は、現在もローカル版の
5メニュー・Step1-4の**存在自体を完全に隠す**。これはIssue #126 P1の
「ブラウザ側にローカルと同じ5メニューの存在と順序を表示する」という要求を満たしていない。

ただし、この隠蔽は意図的な多重防御である：
`tests/test_simple_workflow_cleanup.py::test_read_only_ui_skips_startup_writes_and_auto_resume`
と `tests/test_outreach_read_only_isolation.py::test_nav_hides_outreach_pages_when_read_only`
が明示的にこの挙動をピン留めしており、理由は「Feature Flag設定ミスだけでGmail下書き・Job操作
ボタンが到達可能になる単一障害点を作らない」ため（テストのdocstring参照）。

**解決方針**：ピン留めされた挙動は変更せず、クリック不可能な静的テキスト
（`st.caption`/`st.expander`のみ、新規`st.radio`/`st.button`/`st.file_uploader`/
`write_repositories_for`呼び出しは追加しない）で、5メニュー名・ロック理由・
Step1-4の順序・母集団定義の違いを表示した（PR #127）。

## 稼働中の重複を避けた箇所

- PR #117「Make Cloud Run READ ONLY UI independent of audit data」は、タイトルから
  本ギャップに対応するPRだと推測されたが、実際のdiffを確認した結果、
  PR #118/#121/#122より前の、監査データ依存排除を目的とした別の変更であり、
  現在はCONFLICTING（dirty）。本ギャップとは無関係なので触れていない。
  要否はメンテナのPRレビューで判断してほしい（supersededの可能性がある旨をPR #127に記載）。
- ブランチ`work/cloudrun-readonly-db-deps-20261019`（PR #117のソース）には既存worktree
  `~/ClinicLead-Readonly-UI`が存在し、別セッションが使用中の可能性があるため、
  このブランチへの直接pushは行わなかった。

## 未対応・承認が必要な残課題

| 項目 | 状態 | 次アクション |
|---|---|---|
| PR #92（Google Maps PC1） | MERGEABLE/CLEAN、CI pass | レビュー・マージ判断のみ。新規実装不要。 |
| PR #110（Comdesk重複名寄せ） | MERGEABLE/UNSTABLE、CI fail | CI失敗原因の調査が先。新規実装は今回行わない（重複防止のため）。 |
| PR #101（オンデマンドHP設計書） | CI fail | ドキュメントのみ。CI失敗原因確認が先。 |
| PR #99（GAS/CRM READ ONLY preflight） | CONFLICTING | 本依頼の中心スコープ（メニュー/検索/Comdesk）外。rebaseはメンテナ判断。 |
| 厚生局マスターのCloud Run経由差分取込 | 未着手 | ローカル専用のまま。Cloud Run書込を許可する段階で新規に設計が必要（P2, 要承認）。 |
| 実際のCanary/本番でのIAP許可・拒否、全国検索の実ブラウザ照合 | 未確認 | 本依頼では実Canary操作を行っていないため「未確認」。別ステップでの確認が必要。 |

## 本番/危険操作（未実施・承認待ち）

DB DDL/DML/Migration、GCP Secret/IAM/Cloud Run/Job/トラフィック変更、PRマージ、実Gmail送信、
実HP調査実行、実Comdesk外部同期は一切実施していない。
