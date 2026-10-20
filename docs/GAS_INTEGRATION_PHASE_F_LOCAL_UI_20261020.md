# Phase F: ローカル営業管理UI

## 実行方法

```sh
/private/tmp/cloudrun-ci-venv/bin/python -m streamlit run scripts/phase_f_local_ui.py --server.address 127.0.0.1 --server.port 8513 --server.headless true --browser.gatherUsageStats false
```

ローカルURLは `http://127.0.0.1:8513/`。画面上部にLOCAL MOCK ONLYを表示し、合成医院fixtureとsession内状態のみを使う。Gmail OAuth/DWD、Supabase、Sheets、GASには接続しない。Gmail送信・予約送信はない。

## 画面

- 既存「メール送付文章」タブを利用。Clinic 901選択、院長/宛先/記事URL/本文のプレビュー、5項目確認、確認語 `CREATE_GMAIL_DRAFTS`、Draft保存まで既存UIで操作する。
- `src/outreach/local_mock_draft_transport.py` は `mock://` endpointと`local-mock-only` tokenだけを許可する。operation_key再実行は同じMock Draftを返し、同一Clinic UUID + campaignで別operation_keyを使った再作成はHOLDする。
- Mock保存後はGmail状態、保存結果、Draft/Message/Thread ID、HOLD理由を画面本文で確認できる。
- ⑥案件・状態は医院名、Clinic UUID、GAS案件ID、IS/FS、Draft/Scheduled/Sent/Reply、記事、Sheets同期、アポイントをMock/未取得表示で出す。
- ⑦取込プレビューはCSVを全件表示し、元タブ・元行・raw valuesを保持する。合成5行は `INSERT 1 / UPDATE 1 / NOOP 1 / HOLD 2`。重複Clinic ID/UUIDの全該当行はHOLDし、取込ボタンやSheets書込はない。

## Sheets取得境界

`src/outreach/google_sheets_readonly_adapter.py::GoogleSheetsReadOnlyAdapter` は値取得関数を注入するread-only adapter。許可するタブは `案件管理` / `メール管理` / `進捗` の3つだけで、範囲を `A:ZZ` に限定し行番号を維持する。書込methodを持たない。今回のブラウザ検証では実Spreadsheet IDやOAuth scopeを使わず、CSV uploadとMock sourceのみを使用する。

## ローカル状態・DB

案件画面はMock ledger fixtureである。取込CSVはStreamlit sessionに保存し、プロセス終了時に消える。Supabase向けshared draft schemaはPhase Dで隔離PostgreSQL検証済みだが、このUIから隔離PostgreSQLへ接続してはいない。DB未接続の表示をMock/未同期/未取得として明示する。

## ブラウザ検証

`scripts/phase_f_browser_smoke.mjs` は一時Chrome DevToolsをloopbackの `127.0.0.1:9223` に限定して操作する。検証内容:

1. 生成結果タブとClinic 901を選択
2. 本文・医院・宛先・記事URLを確認し、5 review checkboxと確認語を入力
3. Mock API経由のDraft作成結果 `mock-draft-0001` とID表示を確認
4. 同一医院/campaignで異なるoperation_keyを使い、HOLD理由を確認
5. 案件一覧でUUID/GAS案件ID/Mock状態を確認
6. Mock CSVを開き、5件全件とINSERT/UPDATE/NOOP/HOLD集計を確認

既存のHP調査処理や別worktreeは起動・変更していない。

## テストと制限

Phase F最終全体pytest: **2287 passed, 56 skipped**。関連UI/Mock/READ ONLY回帰は **11 passed**。GAS adapter mockは **5 fixture calls PASS**。Chromeで画面操作し、Draft `mock-draft-0001` 表示・別operation_keyの重複HOLD・案件画面表示・全5行のaction集計を確認した。実Draftは作っていない。

本番前にはGoogle SheetsのRead-only OAuth scope/ID、Clinic Master一意照合、実データの列契約、Supabase隔離環境のDB表示adapter、Gmail安全な非本番OAuth/DWD試験を別途承認する必要がある。
