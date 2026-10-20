# P0: Mac通常モード稼働機能 vs Cloud Run 実態表（Issue #130）

基準: `origin/main` = `83dcda06e1db8cd66c1abf5d851b0933444ba811`（2026-10-20）。
分類: **LOCAL_WORKING**（Macで実際に動作）/ **CLOUD_READER_WORKING**（Cloud Run READ ONLYで実機確認済み）/
**CODE_ONLY**（コードは存在するがCloud Run READ ONLY UIから到達不能）/ **DESIGN_ONLY** / **NOT_IMPLEMENTED** /
**BLOCKED_APPROVAL**（コードは用意できるが実行には別承認が必要な外部要因あり）。

| # | 機能 | 分類 | 根拠 | 既存PRの採否 |
|---|---|---|---|---|
| ⑤ 営業対象・Comdesk出力 | CLOUD_READER_WORKING | 検索・30列CSV/XLSX・UUID重複なし・未承認列空欄を実機確認済み（Issue #126） | 完了・追加作業なし |
| ①-a データ準備：Comdesk 1ファイル取込 | LOCAL_WORKING（Mac）/ CODE_ONLY（Cloud） | `import_ui_comdesk_step`/`import_comdesk()`存在。`simple_workflow_ui(read_only=True)`の早期returnでCloud Run READ ONLYからはStep1自体に到達不能 | 既存PRなし。**本PRで新規実装**（`cloud_comdesk_import_ui`、既定OFF、`import_comdesk()`自体は無変更で再利用） |
| ①-b データ準備：Comdesk 2ファイル差分取込 | LOCAL_WORKING（開発DB限定）/ CODE_ONLY（Cloud） | `src/master/comdesk_snapshot_import.py`、既定OFF・開発DB限定・role check・preview-before-apply、テスト済み（PR #116マージ済み） | 再利用可能な安全パターンとして本PRのモデルにした。追加変更なし |
| ①-c データ準備：厚生局CSV取込 | LOCAL_WORKING（Mac）/ CODE_ONLY（Cloud） | `import_master`（Step2）。同じく早期returnで到達不能 | 既存PRなし。**本PR未着手**（次回） |
| ② Google Maps | LOCAL_WORKING（Mac）/ NOT_IMPLEMENTED（Cloud） | Step3/Step4、`import_maps_results`、`_maps_hp_available_count` | **PR #92は再利用不可**（調査で判明：38,837件の本番一括移行は2026-10-17に実行・照合済みの**使い捨てスクリプト**＋クエリ性能修正であり、Cloud Runブラウザアップロード機能とは無関係。`mergeable`/`mergeStateStatus`とも`UNKNOWN`で非活性）。Cloud用UIはゼロから新規実装が必要（次回） |
| ③ HP調査 | LOCAL_WORKING（Mac）/ BLOCKED_APPROVAL（Cloud） | `src/cloud_run/job_control.py`・`hp_executor.py`・`job_dispatch.py`・`scripts/cloud_hp_worker.py`（fail-closedロール分離・advisory lock・budget reserve）はPR #104-106で**既にmainマージ済み・テスト済み**。UIディスパッチコード自体（`simple_workflow_ui`のStep5、`cloud_run_executor`分岐）も**既に完成**しており新規コード不要。唯一のブロッカーは実行に必要な専用Writer/Job-control GCP SA・DBロールの provisioning（IAM/Secret変更＝個別承認対象）であり、コードの不足ではない | PR #113（HP範囲選択の明示化）はCI pass・無関係ではないが別目的（ローカル通常モードの安全化）。再利用対象ではなく独立 |
| ④ Email調査 | LOCAL_WORKING（Mac）/ CODE_ONLY（Cloud） | `src/email_research_ui.py`、早期returnで到達不能 | PR #109（`email_delivery_records`統合）は**CI失敗・`mergeable: UNKNOWN`**、現状では再利用不可。要修正 |
| ⑥ メール送付文章/下書き | LOCAL_WORKING（Mac、`OUTREACH_GMAIL_DRAFTS_ENABLED`で既定OFF制御）/ NOT_IMPLEMENTED（Cloud UI） | `src/outreach/gmail_workflow.py` | PR #67（+3188/−1）・#66（設計書）とも4日間更新なし・大規模・コンフリクト濃厚。再利用には要リベース、今回は見送り |
| ⑦ GAS・Sheets・CRM | LOCAL_WORKING（Mac、GAS側は無変更）/ NOT_IMPLEMENTED（Cloud UI） | — | PR #99、CI pass だが`mergeable`/`mergeStateStatus`とも`UNKNOWN`（再確認要）。次回以降に再利用検討 |

## 今回のPRで実装した範囲

①-a（Comdesk 1ファイル取込のCloud Run Writer向け安全ラッパー）のみ。理由：
- `import_comdesk()`自体（UUID照合・provenance・import-batch idempotency）は**無変更で完全再利用**でき、新規に書くべきロジックが最小。
- 既存の`comdesk_snapshot_import.py`が確立した安全パターン（既定OFFフラグ・プレビュー必須・書込前確認）をそのまま踏襲でき、設計をゼロから起こす必要がない。
- 現行READ ONLY Canary（`CLINIC_READ_ONLY_UI=true`）では`read_only_ui_mode()`チェックにより**常に無効**（新しいパネル自体が描画されない）。Mac通常モードでもフラグ未設定のため何も変わらない。本番影響ゼロで安全に検証できる。

## 次回以降の候補（優先順）

1. ①-c 厚生局CSV取込のCloud Run Writer向け安全ラッパー（①-aと同型パターンを流用できる）。
2. ③ HP調査：コードは完成済みのため、専用GCP SA・DBロールのprovisioning承認が取れ次第、追加コードなしで有効化可能。
3. ② Google Maps：PR #92は使えないため、Cloud Run向けアップロードUIを新規設計・実装する必要がある（最も工数が大きい）。
4. ④ Email調査：PR #109のCI失敗を先に解消する必要がある。
5. ⑥⑦：大規模な既存PRのリベースまたは作り直しの判断が必要。
