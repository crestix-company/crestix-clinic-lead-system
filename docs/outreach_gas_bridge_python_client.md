# GAS連携ブリッジ Python クライアント（Phase 1: インターフェース確定・Mock接続）

対応先：`~/ClinicLeadGASIntegration/bridge/`（GAS側、22/22テストPASS済み、本番未デプロイ）。
GAS側の資料（`HANDOFF.md`、`docs/python_integration_contract.md` 他）を前提に、
Python側のクライアント実装とMock接続試験を行った。

## 実装したもの

| ファイル | 役割 |
|---|---|
| `src/outreach/gas_bridge_client.py` | JSON契約の確定実装。`GasCaseRegistrationRequest`/`GasCaseRegistrationResult`/`GasBridgeClient`。transportを注入する設計で、本番HTTP接続なしに全ロジックをテスト可能。 |
| `src/outreach/gas_bridge_transport.py` | 本番用HTTP実装（`urllib`のみ、追加依存なし）。**本番Web App URLへは未接続**（URLがまだ無い）。 |
| `src/outreach/gas_bridge_ledger.py` | operationKey↔Clinic UUID↔GAS案件ID↔Draft ID↔Thread ID↔Tracking Tokenの対応をローカルJSONLに記録。`gmail_drafts.py`の安全なファイル書込パターン（flock, 0600/0700）を流用（下書き作成ポリシー自体は再利用していない）。 |
| `src/outreach/gas_bridge_orchestrator.py` | 既存の下書き作成結果（`existing_draft_id`）とGASクライアントを束ねる薄い糊。Draft ID優先の重複防止をローカル台帳で補強。 |
| `tests/test_gas_bridge_client.py` | 契約の単体テスト（18ケース）。 |
| `tests/test_gas_bridge_ledger.py` | 台帳の単体テスト（5ケース）。 |
| `tests/test_gas_bridge_transport.py` | HTTP実装の単体テスト（`urlopen`をモンキーパッチ、実通信なし、4ケース）。 |
| `tests/test_gas_bridge_mock_connection.py` | **Mock接続試験**（認証・タイムアウト・部分成功・再試行・重複登録、9ケース）。 |
| `tests/support/mock_gas_bridge_server.py` | テスト専用のGAS側ふるまいモック。本番ロジックの再実装ではない。 |

## 役割分担（変更なし、GAS側Phase 2の方針を継承）

- Clinic UUID照合・`operationKey`生成・重複登録ポリシー：`src/outreach/gas_sheet_bridge.py`（PR #94）を**再利用**。本クライアントは再実装しない。
- Gmail下書き作成：`src/outreach/gmail_drafts.py` / `gmail_workflow.py`（既存）が担う。本クライアントは**下書きを作成しない**。常に`existing_draft_id`を受け取る側。
- 送付文章・テンプレート（`email_template_v2.py`、記事あり/なしテンプレート）・HP調査・Email調査：**一切変更していない**。

## GAS側の未解決事項（勝手に補完していない）

HANDOFF.mdに記載の2点は、そのまま「確認待ち」として扱った：

1. 「メール管理」「日程候補ログ」「フォーム回答」「トラッキングログ」タブの更新元プロセス不明 → 本クライアントはこれらに一切書き込まない。関与しない。
2. `findDraftByOperationKey`のGmail検索制約 → `gmail_recovery_strategy.md`の「優先2」（Message-IDへのoperationKey埋め込み）は**実装していない**。`gmail_drafts.py`のMessage-ID生成には触れていない。本クライアントの復旧は「優先1」（Draft IDをローカル台帳に保存して再利用）のみに依拠する。

## 未実施（本番接続前に必要）

- GAS Web AppのURL・共有シークレットの発行（運用担当者承認待ち）。
- `gas_bridge_transport.py`の実URLへの接続試験（現在は`urlopen`をモックしたテストのみ）。
- `outreach_delivery_repository.py`（別ブランチ`feat/email-outreach-completion-20261017`、他セッション管轄）との統合は未着手。本ブリッジのローカル台帳とは別物。

## テスト結果

Target tests: `test_gas_bridge_client.py` + `test_gas_bridge_ledger.py` +
`test_gas_bridge_transport.py` + `test_gas_bridge_mock_connection.py` + 既存
`test_gas_sheet_bridge.py`（PR #94, 回帰確認） = **44/44 passed**

Full suite（このworktree全体）: **1949 passed, 55 skipped, 0 failed**
（既存テストへの回帰なし）
