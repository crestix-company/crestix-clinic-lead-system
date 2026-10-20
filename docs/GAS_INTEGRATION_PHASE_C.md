# Clinic Lead × GAS 統合 Phase C ローカル実装

状態: HTTP/API adapterとCloud Run deployment profileのローカル設計・fixture検証。Production接続/デプロイなし。

## API boundary

`POST /v1/drafts`はFastAPIで、`SharedDraftCreationAPI.create_or_recover()`だけを呼ぶ。追加のDraft作成ロジックは持たない。厳格なPydantic schema、unknown field拒否、サイズ上限、Bearer認証、`draft:create` role、confirmed Clinic UUID resolver、caller role/clinic/campaign scope、相関ID、PIIを含まない監査イベント、429制限を実装。

HTTP結果はDraft create/recover=200、競合中=202、入力/identity conflict/HOLD=409、依存障害=503。Gmail/APIの結果が不明なclient timeoutは同じoperation keyでのみ人の確認後に再実行し、クライアントは自動再送しない。Gmail adapter側timeoutとcoreの再照合後も判定できないときはHOLD。

Cloud Runは`Dockerfile.shared-draft-api`による別サービス候補で、既存`Dockerfile.cloudrun`/Service/Revisionは変更しない。`SHARED_DRAFT_API_FACTORY=module:function`のcomposition factoryは、Secret Managerから注入されたtoken、`clinic_outreach_runtime`へassumeするSupabase Postgres pool、Gmail read/create adapters、`public.clinics.uuid` resolverを渡す。未設定のコンテナは起動せずfail closed。Cloud Run IAM/private ingressで外部到達を制限し、API tokenはSecret Managerから環境注入、ローテーションする。GASはidentity tokenを既存URLFetch OAuthだけで使う経路を決め打ちできないため、専用Bearer credentialのbroker/rotation ownerを移行前に確定する。

Cloud Run instance間で共有される排他はSupabaseのunique key・transactional lease・fencing token。API limiterもmigration案のatomic Postgres rate windowを使う。HTTP process memoryやSQLiteは本番排他/制限に使わない。Postgresが落ちた時はlimiterが拒否し、Draft coreもreservation失敗でHOLD。migrationは提案のまま未適用。

## GAS / Clinic Lead adapter

`reference/gas_adapter/SharedDraftApiAdapter.gs`は新規・未導入のadapter例。将来の切替対象は既存GASの「Clinic UUID/GAS案件ID/キャンペーンが確定した後、Gmail Draftを作る直前の旧Draft作成ブロック」のみ。`submitForm()`、既存Sheets書込、Calendar処理は今回触れていない。既存履歴からoperation keyを固定生成し、GAS case IDを含めてAPIへ送る。

切替は (1) APIとGmail metadataの非本番read-only検証、(2) Spreadsheet/GAS ownerと対象trigger確認、(3) 停止時間を決め旧Draft writerを停止、(4) API側のread-only監視確認、(5) GAS旧writerをdisabledのまま新APIを単一経路で有効化、(6) 同一operation keyで回収/Sheet preview確認、(7) rollback時は新APIを止めてから旧writerを再開、の順。旧GAS writerと新API writerの並行期間には共通lockがなく完全排他は保証できない。fixture testでも旧writerがAPI leaseを迂回すると二重作成が成立することを確認する。運用停止の確認なしにGAS/Clinic Lead双方を有効化しない。

`src/outreach/shared_draft_ui_adapter.py`は既存メール送付UIのclient/data mapper。医院名・UUID・本文・記事URL・状態・Draft/Message/Thread IDを既存画面に渡せる。既存レイアウト/送信前ISリーダーgateを変えず、送信/予約送信を実装しない。既存UIのDraft経路は自動切替せず、feature-flag付き接続は別レビューで行う。

## State, Sheets, follow-up

Phase Bの`build_sheet_sync_preview()`を再利用し、Sheetsの実書込はない。Draft/Scheduled/Sent/Replied/Trash/UnknownはSupabase operation/event rowsで保持し、source row単位のINSERT/UPDATE/NOOP/HOLD previewを作る。返信・Calendar・MeetはPhase B interfaceを維持し、実操作はない。

## Operations and remaining gates

未解決の移行前項目: runtime factoryとGmail/Supabase adaptersのCloud Run設定、secret rotation owner、confirmed-identity query権限、API token provision/broker、Postgres migration/RLSレビューと別承認、GAS trigger停止/rollback runbook、非本番Gmail metadata verification、Cloud Run IAM/ingress/rate limit設定、Sheet ownerによるprojector列契約。ここではProduction DB/Gmail/Sheets/GAS/Calendar/Meet/Cloud Run操作を行わない。

## Local verification / completion

`tests/test_shared_draft_http_api.py` adds unauthenticated/permission/scope rejection, UUID mismatch, schema extra-field rejection, correlation ID/audit, same-key retry, concurrent calls, timeout and DB-reconcile behavior, Supabase limiter outage, old GAS bypass risk, and UI field mapping fixtures. The selected Phase A/B regression set plus this file completed **155 passed**. `git diff --check` and Python compilation passed. Tests use fake Gmail/mailbox and fake Supabase stores only.

Phase C is **local implementation complete; production integration not ready**. The HTTP contract, client boundaries, Cloud Run image profile, Postgres lease/rate-limit proposals, and local behavior are present. The deployment-specific dependency composition, credentials, schema approval, and controlled writer cutover are explicit pre-production gates.
