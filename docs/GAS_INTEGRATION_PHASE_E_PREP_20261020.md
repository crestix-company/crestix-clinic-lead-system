# GAS連携 Phase E準備・ローカル統合検証

対象: `feat/gas-outreach-integration-v2`  
範囲: ローカル実装・Mock API・隔離DBのみ。本番DB/Gmail/GAS/Sheets/Cloud Runへの接続、書込、デプロイは行わない。

## Gmail認証方式の比較と推奨

| 観点 | 既存Gmail OAuth | Workspace DWD |
|---|---|---|
| 認証主体 | Gmail利用者のOAuth同意・refresh token | Cloud Run service accountがWorkspace管理者の委任を受ける |
| 常時稼働 | refresh tokenの保管・失効・再認可の運用が必要 | ユーザー操作なしでサービス稼働しやすい |
| 権限境界 | 個人/共有メールボックスの実利用者権限 | Workspace管理者が委任scopeと対象mailboxを管理 |
| 鍵・secret | refresh token/client secretをSecret Managerで保護 | service account鍵を発行せず、実行identityから短期tokenを取得する形を優先。DWD主体・scope設定は管理者管理 |
| 適用候補 | Clinic Leadの人間操作・既存運用との継続性 | 専用Cloud Runサービスから許可mailboxのDraftを作る用途 |

**推奨:** 専用の非本番Cloud Run検証ではDWDを候補とする。現在のruntime factoryはSecret Manager注入JSONを要求するため、鍵JSONを長期secretとして持つ構成のまま本番採用しない。可能ならCloud Run service identityの短期認証でDWD assertionを行う方式へ改修し、委任scopeは `gmail.readonly` と `gmail.compose` に限定する。DWDのWorkspace管理者設定、mailbox許可、Secret Manager格納方式、OAuth consent/refresh token運用を管理者が承認するまでは認証情報を発行・変更しない。既存OAuth方式を本番で止める作業もしない。

## UI Mock API画面操作

`tests/test_shared_draft_ui_apptest.py` は既存「メール送付文章」ページをStreamlit AppTestで操作する。合成医院1件を選択し、医院情報・本文を確認、担当者と5確認項目を入力、「CREATE_GMAIL_DRAFTS」を入力してMock APIへPOSTする。Mock応答のDraft/Message/Thread IDと保存結果を画面session stateに表示する。`requests.post` はテスト内fixtureに差し替え、ネットワーク・Gmail・DBは利用しない。

## Supabase Migration本番適用前チェック

`supabase/migrations/20261020100000_shared_gas_clinic_draft_api.sql` は提案のみ。隔離Postgres検証はPhase Dの結果を維持し、本番適用しない。

1. 本番相当の別プロジェクト/DB cloneで既存 `outreach.clinic_draft_guard`、role、clinic RLS前提を確認し、DDLを再適用可能な空DBと既存データ双方で検証する。
2. `SECURITY DEFINER`各関数のowner、固定 `search_path=''`、完全修飾参照、PUBLIC/anon/authenticated/clinic_runtimeからのEXECUTE剥奪、runtime roleだけのEXECUTEをカタログ検査する。
3. runtime roleがledger以外のテーブルへ書けないこと、RLS FORCE、USAGE/SELECT範囲、`public.clinics`のSELECTが既存RLS内に収まることを確認する。
4. `UNIQUE(clinic_id,campaign_id)`、operation key、GAS case ID、Draft ID/Message IDの一意性を既存データと照合する。異常行はmigration前にReviewへ分離する。
5. 競合予約、期限切れleaseのfencing、旧fenceからの完了拒否、同一operation keyのidentity不一致、roleの直接DML拒否を並列試験する。
6. transaction pooler利用時の `SET ROLE` 接続初期化、接続再利用、statement timeout、pool上限を検証する。
7. rollback DDLとデータ保持/復旧手順、バックアップ、適用担当者と承認者を用意する。Production適用は別途明示承認後のみ。

## Cloud Run fail-closed確認表

| 不足/誤設定 | 期待動作 | 確認方法 |
|---|---|---|
| Cloud Run IAM未認証 | Cloud Run ingress前段で拒否 | 検証サービスで認証なし401/403。サービスに `allUsers` invokerを付与しない |
| アプリtokenなし/無効、scope不足 | API 401/403、Draft core/Gmail未到達 | HTTP mock認証テスト |
| Secret Manager secret参照不可/未設定 | revision起動失敗またはruntime factory起動失敗。Gmail未初期化 | secret参照を外した構成検証 |
| ingress誤設定 | 外部経路を制限。GASを含む必要caller経路が通るか別途確認 | isolated projectのIAM/ingress matrix |
| Supabase DSN接続不能/権限不足 | pool preflight失敗、API未起動。既存Draft回収不能時はUNKNOWN/HOLD | 接続不能・関数権限不足fixture |
| Gmail OAuth/DWD不備・mailbox不一致 | `getProfile` preflight不一致でruntime起動失敗。作成要求なし | fake service profile mismatch test |
| Clinic Lead `SHARED_DRAFT_API_ENABLED`なし | 共有APIモードにならず、通常のGmail作成も明示 `OUTREACH_GMAIL_DRAFTS_ENABLED` がfalseなら停止 | env未設定のUI確認 |
| API mode=trueだがURL/tokenなし | 保存ボタン無効、legacy Gmailへfallbackしない | UI test/設定欠落確認 |
| Cloud Run `SHARED_DRAFT_API_RUNTIME_ENABLED`なし/true以外 | runtime factoryはpool/Gmail provider作成前に起動拒否 | factory unit test |
| rate limit/監査/DB lease機能停止 | 429/503またはHOLD/UNKNOWN。再送しない | API層のfailure mock |

Cloud Run設定テンプレートは別サービス・別project前提。`ingress: all` だけでは公開実行を許可せず、`--no-allow-unauthenticated`相当のIAM必須を適用する。GASの呼出経路を確保するためingressのallが必要となる場合でも、invoker主体を限定し、アプリtokenを二重検証する。max instances × pool sizeがSupabase接続枠を超えないよう上限を定める。secret参照にSecret Accessorを実行service accountへ必要なsecret単位で付与し、Secret本文やAuthorization headerをログへ出さない。UIのAPI flagが未設定の場合、共有APIルートは無効であり、既存legacy routeは独立した `OUTREACH_GMAIL_DRAFTS_ENABLED` が明示trueのときのみ有効となる。API-only cutover時にはlegacy flagをfalseに固定し、API flag欠落で旧writerへ黙ってfallbackしないことを環境設定で確認する。

## GAS旧writerから共通APIへの切替・rollback

1. Spreadsheet/GAS owner、trigger一覧、実行中処理、送信/予約状態、operation key契約、対象mailbox、切替時間帯を事前確認する。
2. 本番外でAPI認証・UUID照合・Draft回収・Sheets previewを検証する。本番GASはこの準備段階で変更しない。
3. 停止時間帯に旧GAS Draft writerを停止し、時間主導/フォーム起動/再試行triggerと実行中runがないことを確認する。フォーム送信・既存Sheets更新・Calendar処理は維持する。
4. GmailのDraft/Scheduled/Sent/Trashと既存履歴をread-only照合し、未確定案件をHOLDにする。
5. 新API callerを一つだけ有効化し、clinic UUID + campaign + operation keyで回収/予約を行う。同期はpreview後、一意一致だけを適用候補にする。
6. 監視期間中、旧writerを無効状態に保ち、API結果・Supabase event・Sheets previewを照合する。
7. Rollbackは先にClinic Lead/GASのAPI writerを停止し、実行中API要求が収束するまで待つ。Gmailと台帳のDraft/Scheduled/Sent/Trashを照合し、UNKNOWN/曖昧をReviewへ回す。その後に限り旧writerを再開する。

**重複リスク:** 旧GAS writerが動作中のまま新APIへ切り替えると、旧writerはSupabase leaseを通らずDraftを作れる。したがって共通APIの一意制約・leaseだけでは完全な競合防止を保証できない。同一医院・campaignでも二重Draftが作成され得る。旧writer停止確認のない並行稼働は禁止。GASを変更しない今回の段階では、共通ロックはAPIを通るClinic Lead/GAS caller間だけを保証する。

## Phase Eローカル検証で残す境界

- 許可: local pytest、Mock UI/API、isolated DB fixture、DDL/RLS静的レビュー、runbook/設定案。
- 未実施: 実Gmail、実Sheets/GAS、Production Supabase、認証情報発行/変更、Cloud Run deploy。
- 本番切替条件: DWD鍵なし方式/委任範囲の承認、runtime feature flagとlegacy writer排他策、Migration承認と適用リハーサル、非本番Gmail実Draft回収テスト、Sheets列契約・運用責任者の承認、IAM/Secret/ingressのnegative test、rollback演習。

## 今回の検証結果

- Python 3.12 full pytest: **2284 passed, 56 skipped**。隔離環境 `/private/tmp/cloudrun-ci-venv` を使用。テスト実行でProduction接続なし。56 skipはopt-in構成/外部サービス依存等。
- Streamlit UI Mock API画面操作: **1 passed**。Clinic selection → review fields and checks → typed confirmation → Mock API Draft ID/result display.
- 変更追加後のHTTP/runtime/read-only/UI関連テスト: **11 passed**。
- GAS adapter: **5 fixture calls PASS**、外向きrequestなし。
- Supabase isolated PostgreSQL: Phase Dの隔離DBテスト **1 passed** を再利用。今回full pytestでは環境変数が設定されていないためopt-in DBテストはskip。本番migrationは未適用。
- 初回full runでは既存read-only source assertionがAPI branchを考慮せず1 failure。read_only条件が両経路の最上位disable条件であることを保ち、テストの構造期待のみ更新。再実行全件PASS。
- `git diff --check`: PASS。

**判定: Phase Eローカル準備は CONDITIONAL PASS。** 本番/非本番実Gmail、Production DB、GAS/Sheets変更、Cloud Run deployは未実施。本番化は上記移行条件とDWD方式の承認後に別Phaseとして行う。
