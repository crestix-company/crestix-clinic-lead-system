# GAS・CRM連携：READ ONLY事前チェック（Phase 1 UI）

## 狙い
Clinic Lead Systemの「かんたん操作」「営業対象・出力」「メール送付文章」は既存のまま、サイドバーに「GAS・CRM連携」を追加する。Clinic UUIDとSalesNest CRMの会社IDを、GAS履歴と照合して事前に監査する。

## 画面の操作
候補CSV、GAS履歴CSV、CRM対応表CSVをアップロードし、重複・未確認をプレビューして監査CSVをダウンロードする。現時点では実データの自動収集や本番同期は実装しない。

- 候補：clinic_uuid, clinic_name, campaign_id, recipient_email, template_kind, gmail_draft_id
- GAS履歴：operation_key, clinic_uuid, gas_case_id, gmail_draft_id
- CRM：organization_id, clinic_uuid, crm_company_id
- UUIDの認定とoperation keyは既存 gas_sheet_bridge.py に委譲（二重開発しない）。
- GAS/Sheets/Gmail履歴未確認はHOLD。同一operation keyまたはDraft IDが重複するとREVIEW。
- CRM company が別Clinic UUIDへも割り当てられている場合はREVIEW。
- PREVIEW_OKはCSV内の照合結果のみ。手動の履歴確認は本番接続の許可にならない。
- CSVは最大3MB/5,000件でメモリ内処理、外部通信なし、結果CSVではメール/Draft IDを出力しない。

## 安全・未実装
Production DB DDL/DML・Migration、GAS本番変更、Gmail下書き作成/送信、既存HP/Email Job操作は行わない。

次工程：PR #98の検査→GAS Web App承認とMock E2E→読み取り専用のGmail履歴照合→CRM既存companiesとのIDマッピングを持つ認可済み取込API→実シートの安全な小規模テスト。CRM組織境界を保持し、同じ医院は1会社に紐付け、案件は複数保持する。

tests/test_gas_crm_preview.pyで純粋関数を回帰確認。レビュー・CI完了までmainへマージしない。
