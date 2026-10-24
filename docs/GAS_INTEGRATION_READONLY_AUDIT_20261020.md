# GAS営業メール連携 READ ONLY 調査・設計

調査日: 2026-10-20 (JST)\
基準: `origin/main` = `83dcda06e1db8cd66c1abf5d851b0933444ba811`\
Macローカル完成基準: `4e973b8293c6999b0eebd2a9c6d48f5eda401f28`

## 調査範囲と安全境界

- リポジトリのコード・文書・Migrationは読取のみ。Production DB接続、Gmail OAuth、Cloud Run、GAS Web Appは操作していない。
- Google Drive経由で対象スプレッドシートのメタデータとタブの1行目（見出し）のみ確認した。医院・宛先などのデータ行は取得・出力していない。
- Phase 1時点ではGASコード未提供だったため依頼書/見出し中心の調査だった。Phase 2では参照ディレクトリ内の4ソースを静的に確認した。GASのデプロイ版との一致、トリガー稼働状態、Calendar/Chatの実環境設定は未確認。
- 既存の `docs/OUTREACH_SHEETS_GMAIL_INTEGRATION_DESIGN.md`、`docs/outreach_gas_sheet_bridge_phase1.md`、`docs/outreach_gas_bridge_python_client.md` にある先行設計を読み合わせた。そこに記載の件数は過去の調査時点の記録であり、今回再計数していない。

## 1. 既存GAS機能一覧

依頼書およびシート構造から確認できた機能領域:

| 領域 | 観測されたデータ/根拠 | 検証状況 |
|---|---|---|
| フォーム受付・診療時間入力 | `フォーム回答`（受付日時/案件ID/フォームJSON）、`案件管理`（曜日別診療時間等） | シート見出しと提供GASコードを確認。デプロイ版の一致・実稼働は未確認 |
| 案件・IS/FS管理 | 案件ID、IS担当、担当FSの列 | 提供GASコードで案件ID生成・管理処理を確認。Clinic UUID/clinic_id列や生成処理は確認できない |
| 候補日時・空き検索 | `日程候補ログ`（案件ID/候補番号/日時/対応可能FS）、案件管理の候補列 | 提供GASコードでCalendar FreeBusyと候補生成を確認。実環境のCalendar状態は未照会 |
| Gmail下書き・送信・返信同期 | Draft ID/Thread ID、送信日時/有無、返信有無/日時、自動返信数、メール状態列 | 提供GASコードでDraft作成と送信/返信同期を確認。送信予約照会とMessage ID保存は見当たらず、Gmailライブ状態は未照会 |
| 開封・クリック追跡 | `トラッキングログ`（イベント、信頼度、IPハッシュ等）、案件管理の集計列 | 提供GASコードでtracking token検証・イベント記録を確認。Cloudflare Worker実装/稼働は未確認・変更なし |
| アポ・Calendar・Meet・Chat | Event ID、Meet URL、アポ状態/確定日時、Chat通知状態 | 提供GASコードでCalendar/Meet/Chat処理とEmailAppointmentChatV2の重複防止キーを確認。実環境設定・外部実行は未確認 |
| KPI/リードタイム | `集計`、案件管理の開封・返信・商談間隔、受注項目 | 列構造を確認。計算式・集計精度は未検証 |

今回のシートメタデータでは17タブが確認された。特に `案件管理` は83列、`ClinicLead_GAS連携` は26列の連携契約見出しを持つ。後者を含め各タブの見出しは既存設計資料と整合する。これらの列構成は現状の実装契約であり、変更しない。

## 2. Clinic Lead System既存機能との重複

- クリニック識別・Clinic UUID: `src/outreach/gas_sheet_bridge.py` に保守的一致判定がある。医院名のみ/メールのみでは自動確定せず、医院名と電話または住所の一致、他の識別情報に矛盾がないこと、一意性を要求する。
- 営業文章・記事・宛先候補: `src/outreach/` の生成・記事判定・Email Research機能と `src/outreach_ui.py` の「メール送付文章」画面が存在する。
- Gmail下書き: `src/outreach/gmail_drafts.py`、`gmail_workflow.py` がDraft重複検査、Gmail状態照会、下書き作成の既存導線を持つ。`OUTREACH_GMAIL_DRAFTS_ENABLED` は未設定時OFF。
- Clinic ID/キャンペーン単位の配信台帳: `src/repository/outreach_delivery_repository.py` に予約、状態同期、Gmail ID記録、過去履歴バックフィルの機能がある。PR #112由来のclinic-wide draft guardも先行資料に記録されている。
- GAS連携契約: `gas_bridge_client.py`、`gas_bridge_transport.py`、`gas_bridge_ledger.py`、`gas_bridge_orchestrator.py` があり、既存Draft IDを受け取ってGAS案件登録へ渡す契約を担う。GAS接続先未設定/本番未接続とする既存資料の説明はあるが、今回接続確認はしていない。
- CSV/履歴のプレビューと照合、事前レビュー、営業メール履歴の読取表示が既存UIにある。

したがって、新UI/新しい重複判定/新規Draft作成器を重ねて作らず、既存機能を合成して使う。既存のメール送信・予約送信機能は今回の設計対象から除外する。

## 3. 再利用可能な実装

| 必要機能 | 再利用先 | 制約 |
|---|---|---|
| Clinic UUID候補照合 | `src/outreach/gas_sheet_bridge.py` | 曖昧/矛盾はREVIEW。自動でUUIDを推測しない |
| operation_key | `registration_key()` | clinic UUID + campaign + 正規化宛先 + テンプレート区分。異なるキャンペーンを同一送信と誤認しない |
| GAS登録ワイヤ契約 | `src/outreach/gas_bridge_client.py` | 実在するGmail Draft IDが必須。Draft作成責務を持たない |
| Gmail ID/履歴照合 | `gmail_drafts.py`, `gmail_workflow.py` | Draft/Message/Thread IDを別属性として保持。照会失敗は不明扱いでfail closed |
| 下書き重複防止・予約 | `outreach_delivery_repository.py` とDraft guard | 再利用を優先し、競合はREVIEW/BLOCKED |
| 原票プレビュー | `source_import.py`, `legacy_import_preview.py`, UI | 既存の読み取り機能を使い、原データを上書きしない |
| 下書き内容確認 | `pre_send_review.py`, `pre_send_ui.py` | 下書き保存確認と送信承認を分ける。送信処理を追加しない |

## 4. 新規実装が必要な機能

READ ONLY調査の結果、機能そのものより「既存の部品を壊さず安全に統合する接続・運用の不足」が主な差分。

1. 既存全タブのリビジョン/チェックサム付きインベントリを生成し、原票の重複・スナップショットを保持したまま差分を説明するインポートプレビュー。
2. Clinic UUID・clinic_id・GAS案件ID・operation_key・Gmail Draft/Message/Thread IDを、証拠と照合状態付きで紐付けるレビュー画面/レポート。
3. 送信予約・送信済み・返信済みの状態機械とGmail/シート間の矛盾キュー。シートの文言をGmail上の事実と同一視しない。
4. 複数ケース/複数送信/複数キャンペーンを保持する履歴一覧、保留理由、手動承認・解決履歴。
5. Calendar Event ID/Meet URL/Chat状態を外部事実として取り込む将来の読取アダプタと競合レビュー。今回は接続・副作用を実装しない。
6. 再開可能な同期ジョブ、source row単位のエラー記録、監査ログ。同期は副作用境界を定めた別PR以降で設計承認後に実装する。

## 5. データ統合設計

### 原票保持

- タブごとに別ソースとして、スプレッドシートID、タブ名、取得時revision（取得できない場合は未設定）、取得時刻、チェックサム、順序付きヘッダー、原行番号、セル文字列、行ハッシュを保持する。
- 数字に見える案件IDやDraft IDを数値変換しない。フォームJSONは文字列のまま保持する。日時やJST表現も原票列と正規化列を分離する。
- 同じ医院や同じDraftが複数行/複数タブにある場合も消去・自動マージしない。スナップショット・履歴・同一送信の再取込をそれぞれ独立に追跡する。
- 一括importはプレビュー、件数/ハッシュ確認、人手承認、専用ロール/権限確認を別段階とする。今回の調査は原票をDBへ取り込まない。

### 識別とGmail ID対応

照合順は強い明示キーを優先する: (1)既存のoperation_key完全一致、(2)GAS case ID完全一致、(3)Clinic UUID完全一致、(4)clinic_id完全一致。医院名+電話/住所は候補生成の補助であり、確認済み一意一致かつ他属性に矛盾がない場合だけ承認候補にする。宛先メールや医院名だけでは結び付けない。

Draft ID、Message ID、Thread IDは異なるIDとして保存する。Draft ID完全一致はドラフト同一性、Message ID完全一致は送信メッセージ同一性、Thread ID一致は会話グループ一致の証拠とする。Thread IDだけで複数送信を一つにまとめない。ID間の対応はGmail APIの観測またはシートの明示リンクを根拠にし、値を相互代用しない。

## 6. 重複判定ルール

「同じ宛先への複数回の正当な送信」と「同じ履歴行/イベントを複数回取り込んだこと」を別問題として扱う。

| 判定対象 | 推奨キー | 自動処理 |
|---|---|---|
| 業務上の送信操作 | Clinic UUID + campaign_id + 正規化宛先 + template_kind の operation_key | 同一キーが既存なら新規作成を止め、既存案件/下書きを再利用するかレビュー |
| 同一医院への別回/別キャンペーン送信 | 別operation_key、個別GAS case ID、別のDraft/Message ID | 設計上は別履歴として保持する。ただし現行 `create_gmail_draft()` のclinic-wide guardは別campaignも停止する。別campaign再送は明示承認・ポリシー変更なしに許可しない |
| 原票行の再取込 | source batch + tab + original row number。batch自体はrevision/checksumで識別 | 同一バッチ再実行は冪等化。原票行は残す |
| Gmail履歴の同一実体 | Gmail Draft ID、Message IDのそれぞれに個別一意性。Thread IDは非一意 | 同じIDの再観測は既存事実の照合。内容/宛先/Clinic不一致なら競合として保留 |
| トラッキングイベント | source_system + source_event_key（安定イベントキー） | 同一イベントだけ再取込抑止。イベントキー不在/不安定なら自動統合せず要確認 |
| 宛先/件名だけの一致 | 正規化宛先 + 件名は弱い候補キー | 自動削除・自動統合は禁止。Draft候補としてレビュー |

一般的な重複モデルではclinic_id単独のキーは別campaignを阻害し、宛先単独は共有メールを誤統合するため、operation_keyと区別する。ただし現行draft作成は安全側にclinic_id単位で一度だけ予約する `outreach.clinic_draft_guard` を適用している。この制約を緩める判断は別送信ポリシーの明示承認と専用PRを必要とする。既存DraftのIDが見つかっても、Clinic/案件/送信内容が矛盾する場合は再利用を自動確定せず保留にする。

## 7. UI追加案

既存「メール送付文章」配下に小さく追加し、既存メニューや営業導線の大幅変更は避ける。

1. 「GAS連携履歴」読取タブ: ケース/キャンペーン/clinicの絞り込み、出典、Draft/Message/Thread ID、Gmail/Sheet別状態、最終確認時刻。
2. 「取込プレビュー」: タブ・行数・チェックサム・重複候補・列マッピング・保留件数。PIIは必要な列のみ画面上でマスクし、CSV出力やログには出さない。
3. 「照合レビュー」: 既存キー、候補、矛盾理由、承認者、承認時刻を表示。曖昧候補は一括確定不可。
4. 初期設定はFeature Flag OFF。今回のPRは表示もDB書込も追加せず、後続PRの提案に留める。

## 8. DB変更の必要性

既存delivery ledgerは下書き予約とclinic/campaign状態に使えるが、GAS原票、複数案件/送信履歴、原票由来、状態遷移、外部Appointment、保留/解決を一括で表すには不足がある。従って本格的な取込・照合を実装する段階では追加サイドカー構造が必要。ただし既存DBの破壊的変更は不要。

`supabase/migrations/20261019160000_outreach_full_sheets_gmail_integration.sql` にサイドカー案がリポジトリ内に存在する。これは作業ツリーで未変更で、今回適用していない。Productionへ未適用であることは、DB確認をしていないため保証できない。適用状況の確認自体も今回は行っていない。後続のmigration PRではmigration履歴の整合、専用ロール、RLS/ACL、テーブル別権限、既存clinic-wide draft guardとの整合を個別にレビューする。

## 9. PR分割方針

依存順で次のPRに分ける。今回の変更はこの調査設計書のみ。

| PR | 内容 | 書込/外部副作用 | 完了ゲート |
|---|---|---|---|
| 0. 調査・契約文書 | 本書、GAS側コード/trigger所有者、設定、列契約、状態語彙の確認記録 | なし | GASの更新元・状態定義・権限境界の確認 |
| 1. Read-only inventory | revision/checksum/source-row preview、ID間照合レポート、fixtureテスト | ローカル/検証データのみ | PII非出力、原票不変、再実行同一結果 |
| 2. 重複判定・状態モデル | 複数送信と再取込を分ける純粋関数、競合/保留、ID別状態機械 | ローカル/検証DBなし | ケース表テスト、既存Draft guardとの統合設計承認 |
| 3. DBサイドカー | 追加テーブル、RLS/ACL、専用ロール、監査証跡 | Migration/DB変更あり | Migrationレビューと実行承認が別途必要。本依頼外 |
| 4. Read-only UI/Importer | Feature Flag OFFの履歴・照合・preview | 検証環境読取のみ | 既存UI回帰、権限境界、PIIマスキング |
| 5. 非本番同期canary | mock/stagingでの再開・エラー回復 | stagingのみ | 運用承認、冪等性・ロールバック実証 |
| 6. Draft/GAS接続 | Draft/GAS side-effectを別ゲートで審査。送信経路は追加しない | 外部変更あり | 運用承認とcanary。Production展開はさらに別判断 |

各PRは機能単位でレビュー可能にし、Migration・Draft作成・GAS書込・本番操作を一つのPRに束ねない。PR作成/マージ/デプロイはいずれも今回行わない。

## 10. テスト計画

Phase 1時点ではテストを実行していなかった。後続PRのfixture/mock限定テスト計画:

- Clinic一致: UUID完全一致、電話/住所の一意一致、候補複数、医院名だけ、メールだけ、矛盾情報を検証。
- ID照合: Draft/Message/Threadの各完全一致、Thread共有、ID交差不一致、同一宛先で異なる案件、削除/Trash/不明状態を検証。
- 重複: 同じclinic/campaign/recipient/templateの再要求、別campaign/別テンプレート、同じGmail IDの再取込、複数行/複数タブに記録された履歴を別々に保持することを検証。
- importer: 数字文字列、JST日時、空欄、引用符入りJSON、同一CSVの再実行、部分失敗・中断再開、行ハッシュ安定性を検証。
- 状態遷移: DRAFT→SENT→REPLIED、SCHEDULEDの根拠、取消/Trash、GmailとSheetの矛盾を競合として保留することを検証。
- UI/ACL: Feature Flag OFF、読取専用、非許可role拒否、PIIをログ/例外/エクスポートに含めないことを検証。
- Calendar/Meet/Chat: モックで外部ID・複数イベント・取消・重複通知を確認。本番API接続は別承認まで行わない。

## 11. Production影響・安全チェック

| 対象 | 結果 |
|---|---|
| GAS/Spreadsheet/Cloudflare Worker変更 | なし |
| Gmail送信/予約/下書き変更 | なし。Gmailのライブ照会も今回実施していない |
| Production DB読書き/Migration | なし |
| Cloud Run操作/デプロイ | なし |
| PRマージ | なし |
| 個人情報を含むシート行の読取/記録 | なし。メタデータとヘッダーのみ読取 |
| ローカルテスト実行 | なし |
| 既存ソース/設定の変更 | なし。追加したのは本書のみ |

Productionへの影響は本調査によるものはない。既存のGmail下書き有効化状態、本番migration適用状態、GASの稼働状態は確認しておらず、保証の対象外。

## 12. 推奨実装順序

1. GASソース/デプロイ/trigger/権限と、シート各列の更新元、状態遷移定義を所有者立会いでREAD ONLY確認する。
2. PIIを含まない小さな人工fixtureで、ID優先照合・operation_key・原票再取込・Gmail状態機械のテストを設計する。
3. Read-only inventoryと照合レポートを実装し、全データを消さずに曖昧/矛盾をレビューできる状態にする。
4. 競合キュー/サイドカーDBの最小スキーマ、RLS/ACL、専用roleをレビューする。
5. Feature Flag OFFの読取UIとstaging同期を追加し、既存UIと既存Draft guardの回帰を確認する。
6. Gmail Draft再利用/GAS登録の接続を限定canaryで審査する。メール送信・予約送信、GAS/Sheet/Worker変更は別途明示的に承認されるまで対象外とする。

## 4e973b8 からの機能差分

`git diff 4e973b8..origin/main` のファイル一覧は10ファイル。差分はCloud Run parity/read-only menu・prefecture search、`supabase_adapter.py` の改善、関連テストと文書である。`src/outreach/**`、`src/outreach_ui.py`、`src/repository/outreach_delivery_repository.py`、`supabase/migrations/**`、GAS bridge/重複判定/営業メール関連テストには差分がない。

このため、Mac完成基準からの営業メール/GAS統合機能差分は今回確認した範囲で**なし**。`origin/main` には営業メール連携機能の追加・削除・挙動変更がなく、調査基準として採用できる。追加されたCloud Run差分は既存の別機能であり、この統合の依存条件にはしない。

## Phase 1: 実コード照合・契約確定 (2026-10-20)

### GASコードの提供状況

Phase 2で `reference/gas_existing/Code.gs`, `Index.html`, `EmailAppointmentChatV2.gs`, `CaseColumns.gs` を読み取り、以下の関数契約・列・処理を照合した。参照ファイルはローカルの提供スナップショットであり、デプロイ中GASとの一致やトリガー稼働状態は確認していない。

#### GAS実コードで確認できた仕様

| 機能 | 実コード上の根拠 | 確認結果 |
|---|---|---|
| Web受付 | `doGet`, `doPost`, `getInitialData`, `submitForm`, `validateForm_` (`Code.gs`); `Index.html`の入力・送信処理 | POST受付後に`yyyyMMdd-HHmmss-`+3桁乱数の案件ID、UUID由来32桁追跡トークンを生成。Clinic UUID/clinic_id/operation_keyはGAS案件作成時に生成・保存していない。 |
| 日程候補 | `findCandidateSlots_`, `applySchedulingConstraints_`, `getFreeBusyCalendarInfo_`, `getClinicCandidateIntervals_`, `mergeContinuousFsAvailability_` | Clinic/FSの診療時間・例外・Calendar FreeBusyから候補を構成。候補ログは案件IDで関連付け。 |
| Gmail下書き | `submitForm`内の`GmailApp.createDraft`; `saveCase_` | Draft作成時に`draft.getId()`を「下書きID」、Draftメッセージの`getThread().getId()`を「下書きThreadID」として保存。Draft内message IDは保存していない。 |
| 送信/返信同期 | `syncMailStatus`, `getOwnEmails_`, `isAutoReply_`, `updateCaseMailStatus_` | 5分トリガー。まず保存Thread IDを取得し、なければ過去90日・宛先・件名で検索。作成時刻-2分以降、件名・自社送信者・宛先一致で送信を推定。最初の送信後の受信Messageを返信として分類し、自動返信は除外。Message IDは保存しない。 |
| 送信予約 | `syncMailStatus`等 | GAS参照4ファイルにGmailの予約送信照会・予約状態列・予約作成処理は見当たらない。送信予約の有無はGASデータから判定不能。 |
| 追跡 | `buildTrackingLinks_`, `validateTrackingToken_`, `appendTrackingEvent_` | トークンは案件と対でURLに含まれ、OPEN/CLICKを追跡ログへ記録。トークン自体はGmail IDでもClinic IDでもない。 |
| CaseColumns | `CASE_COLUMNS_VISIBLE_HEADERS`, `CASE_COLUMNS_EDIT_WATCH_HEADERS`, `setupCaseColumns`, `refreshCaseLeadTimes` | 17列を表示し、案件ID/送信・返信/アポ列を監視。整形処理はバックアップ、列移動、非表示列、トリガーを伴う書込機能であり、今回実行していない。 |
| 即時Chat通知V2 | `setupEmailAppointmentImmediateChatV2`, `emailAppointmentImmediateChatScheduledCheckV2`, `sendPendingEmailAppointmentChatsV2_`, `buildEmailAppointmentChatSentKeyV2_` | 1分トリガーで確定アポを走査し、案件/通知内容をキーに重複抑止・再試行するコードがある。設定/Webhookや実トリガーの動作は未確認。 |

#### ID対応と状態解釈

| ID/状態 | GAS上の意味 | Clinic Lead側との関係 |
|---|---|---|
| 案件ID | GASの受付ごとに採番する文字列。`案件管理`, `メール管理`, `フォーム回答`, `トラッキングログ`, 日程候補に外部キーとして流通 | clinic_id/Clinic UUIDとは別。医院単位ではなく受付案件単位 |
| 追跡トークン | 案件ごとの追跡URL検証用ランダム値 | Clinic UUID/operation_key/Gmail IDと対応しない。案件IDとの同一行対応を根拠に保持 |
| Gmail Draft ID | `GmailApp.createDraft()`返却のDraft resource ID | Message IDと別属性。GASの「下書き作成」記録だけで現存状態を保証しない |
| Draft Thread ID / 送信Thread ID | Draft配下Messageの会話Thread ID。同期時に送信発見したThreadも同列に記録 | Threadは複数Messageを含みうる。送信Messageの一意識別子にはならない |
| Gmail Message ID | 参照コードで永続化していない | Gmail実照会がない限り不明。GAS案件ID/Draft ID/Thread IDから捏造しない |
| Clinic UUID / clinic_id / operation_key | GASの列/生成/更新コードでは確認できない | 既存Clinic Lead側の照合・操作キーとして別途保持。GAS case IDから推測しない |
| 送信予約済み | GASでは照会していない | 不明状態。送信済み/下書き/返信と混同せず、プレビューは自動確定しない |
| 下書き作成/送信済み/返信あり | GAS Sheet上の同期値 | Sheet観測として記録。Gmail APIのライブ状態や現在のDraft存在とは区別 |

#### 重複防止ルール（Phase 2確定）

1. **同じ原票行の再観測**: `(source spreadsheet, tab, revision/checksum, source row number)`および行hashを使って再取込候補を示す。行は削除/統合しない。同一checksumは同じスナップショットの再観測として扱う。
2. **同じGAS案件**: GAS案件IDの完全一致のみ案件重複の明示根拠とする。同案件IDでも他列が違えば競合としてREVIEW。
3. **Gmail ID**: Draft IDとMessage IDは別々に重複検出する。Thread ID共有は会話関連の証拠であり、同一送信判定に使わない。GASは送信Message IDを出力しないので、シートの送信済み記録はMessage ID照合を満たさない。
4. **状態**: 現在GmailのDraftが存在することを確認した状態、`in:scheduled`等の予約照会で確認した状態、Sent Messageを確認した状態、返信Messageを確認した状態を別々に記録する。Sheetの「下書き作成」「送信済み」「返信あり」はそれぞれ`SHEET_RECORDED_*`証拠に限定し、Gmail未照会時は`UNKNOWN/REVIEW`。状態の矛盾・API照会失敗はBLOCK/REVIEWとして新規処理を止める。
5. **弱い一致**: 宛先、件名、Thread ID、医院名のみで自動マージ/削除しない。同一宛先への異なるGAS案件は別履歴として残す。

#### Phase 2のローカル実装

`src/outreach/gas_readonly_inventory.py` に、提供済みのタブ行をメモリ上で受けて文字列のまま保持するインベントリと照合プレビューを追加した。タブchecksum・行番号・行hashを出し、案件/Clinic ID/追跡トークン/Draft ID/Thread ID/Message IDを別属性で表示用に構成する。Message IDがない場合やGmail状態未照会の場合は`UNKNOWN/REVIEW`とする。ネットワーク・DB・Gmail/GAS/Sheetsアクセス、書込・送信・予約は実装していない。

参照GASの対象タブと提供形式はローカルCSV/JSON相当の行データを渡す契約で、Google Sheetsへの接続や画面はこのPhase 2では追加していない。実コードがない旧記述を本節により更新した。依頼末尾は「6. 人工fixture」で途切れていたため続きは未確認。Phase 2の人工fixtureテストは4件追加し、単一ファイルだけ実行した。

### Clinic Lead Systemの実装対応表

| 対象機能 | 現行コード上の具体箇所 | 確認できる契約/不足 |
|---|---|---|
| GAS案件IDとClinic UUIDの照合 | `src/outreach/gas_sheet_bridge.py`: `LegacyCase`, `ClinicIdentity`, `resolve_clinic()`。`tests/test_gas_sheet_bridge.py` | UUID候補を名前+電話または住所の一意一致で確定。名前/メールだけ、複数候補、院長名/メール/住所/電話の矛盾はREVIEW。Clinic UUID→clinic_idは別の識別子解決で、混同しない。既存の案件IDが同じ医院の別案件を意味する可能性があるためClinic UUIDと別列で保つ |
| operation_key/同一業務操作 | `registration_key()`, `plan_registration()`。`tests/test_gas_sheet_bridge.py` | SHA-256の操作キーはClinic UUID + campaign + casefoldした宛先 + template kind (`LISTED`/`NOT_LISTED`)。履歴未確認ならBLOCKED、既存一致ならREVIEW。GAS案件IDやGmail IDの代替にはならない |
| GAS連携クライアント | `src/outreach/gas_bridge_client.py`: `GasCaseRegistrationRequest`, `GasBridgeClient.register_case()`, `parse_response()`, `build_auth_fields()`; `gas_bridge_transport.py`: `make_http_transport()`; `gas_bridge_orchestrator.py`: `register_with_existing_draft()`; `gas_bridge_ledger.py`: `record_result()`, `find_by_operation_key()`。Mock: `tests/test_gas_bridge_mock_connection.py`, `test_gas_bridge_transport.py`, `test_gas_bridge_ledger.py` | 既存Draft IDを受け取る契約。GAS case ID/draft/thread/tracking tokenの対応をlocal JSONLへ記録。リトライは同じoperation key・既存draftを再利用する。テストtransportはモック。本番GAS URL接続を今回確認していない |
| Gmail Draft/Message/Thread ID | `src/outreach/gmail_drafts.py`: `ExistingDraft`, `ExistingMessage`, `list_existing_drafts()`, `list_existing_messages()`; `gmail_workflow.py`: `DraftWorkflowResult`, `_reconcile_existing_ledger()`, `create_gmail_draft()`; repository `record_draft_result()`, `sync_status()` | Clinic LeadはIDを別々に保持。GASはDraft ID/Thread IDを記録するがMessage IDは記録しない。Thread IDは会話単位で複数Messageを含みうるため送信一意キーに使わない。GASコードの具体的な対応は上記「ID対応」を参照 |
| Gmail送付履歴/予約送信 | `gmail_workflow.py`: `read_gmail_state()`, `classify_target_state()`, `_reconcile_existing_ledger()`; `SCHEDULED_QUERY`; `tests/test_outreach_gmail_workflow.py` | Draft、当日Sent、宛先別Sent history、`in:scheduled`、Trash、返信を照会し、履歴照会失敗はREVIEW。既存SCHEDULED/SENT/REPLIED/UNKNOWN等は再下書き阻止。Sentは宛先だけでなくclinic id/UUID/outreach id優先で矛盾を検出。Gmailの実状態は未照会 |
| 送付済み医院の重複除外 | `classify_target_state()`、`BLOCKING_STATUSES`、`outreach_delivery_repository.py`: `clinic_statuses()`, `reserve_draft()`、`outreach.clinic_draft_guard`; `tests/test_outreach_gmail_workflow.py`, `test_outreach_history_workflow.py`, `test_gmail_history_backfill.py` | 履歴はclinic/campaign台帳に保存する。現在のdraft予約はDBのclinic-wide unique guardでclinic_idにつき1件を競合直列化し、別campaignでも既存履歴があれば止める。`registration_key()`のcampaign差分があることは再送許可を意味しない。clinic UUID欠損やID不一致は確認保留とする |
| メール文章生成 | `src/outreach/generation.py`: `generate_one()`, `generate_batch()`; `compose.py`; `email_template_v2.py`: `render_preview()`; tests `test_outreach_email_template_v2.py`, `test_outreach_mail_send_text.py` | Clinic Lead側に記事区分等に応じた文章生成・検証がある。カレンダーAPIのFreeBusy照会はこの実装ではなく、`calendar_confirmed` は候補日時の確認ゲート。生成はGASフォーム/テンプレートとの同等性を保証しない |
| 一括下書き作成 | `src/outreach/auto_draft_batch.py`: `plan_auto_drafts()`, `run_approved_batch()`; `auto_draft_ui.py`; `gmail_workflow.create_gmail_draft()`; tests `test_auto_draft_batch.py`, `test_outreach_gmail_draft_create.py` | 最大25件。全候補を重複確認し、明示操作後も1件ごとにGmail/DBを再確認。UNKNOWN/REVIEW/DISABLED/例外で停止、同じバッチを自動再実行しない。Gmail下書き作成機能は存在するがFeature Flag既定OFF。今回作成していない |
| 開封/クリックトラッキング | `src/outreach/sync_plan.py`: `shape_tracking_event()`, `event_idempotency_key()`; migration proposal `outreach.engagement_events`; `tests/test_outreach_full_integrations_local.py` | Clinic Lead側はOPEN/CLICKイベント形状、source event key、PIIを抑えたメタデータの純粋関数/DB案を持つ。GAS tracking tokenを受け取るlocal bridge ledgerはある。一方、Cloudflare WorkerやGAS tracking endpointとの稼働同期・再処理ループは今回確認できず、実装済みとはみなさない |
| メール経由アポ確定/Calendar/Meet/Chat | 既存delivery migration proposal `outreach.calendar_appointments`; `FIELD_AUTHORITIES` は `src/outreach/sync_plan.py`; GAS bridge requestは日時候補値のみ | Clinic Lead側に実処理/一意制約/通知outboxは確認できない。`calendar_appointments` は提案スキーマ。GAS提供コード側はCalendar/Meet/Chat機能とChat V2の通知キーを確認したが、外部実環境は未検証 |

### 二重処理を防ぐデータ契約

この契約は将来の実装仕様。今回有効化/接続はしていない。

1. **Clinic UUID** はClinic Masterの医院識別子として保持し、GAS案件ID、clinic_id、メール宛先とは別々の型/列に置く。Clinic UUIDの照合は一意で証拠付きの場合だけ承認。曖昧一致は保留し、推測で紐付けない。
2. **GAS案件ID** は元データ値を文字列で保持し、`(source_system, gas_case_id)` を案件の外部一意キーにする。案件IDがない場合もClinic UUID/operation keyから案件IDを生成したことにしない。複数GAS案件は複数caseとして残す。
3. **operation_key** は「同じ業務操作の再実行」識別子。既存値は不変。値が未提供・campaign/template定義が不明なら自動生成せずREVIEW。別campaignの新規接触は履歴上別操作候補だが、現行Clinic Leadのclinic-wide guardで停止し、人の別途承認が必要。
4. **Gmail Draft ID / Message ID / Thread ID** は別列・別一意制約。Message IDを送信単位の識別、Draft IDをdraft resourceの識別に使う。Thread IDは返信を束ねる補助参照のみ。宛先/件名一致は候補に留め、同一実体の証拠にしない。
5. **送付履歴の二重取込** はsource batch revision/checksum + tab + source row numberで検出し、各原票行を保持する。Gmailの同一Draft/Message再観測はprovider IDで冪等化。重複候補の原票を削除/マージしない。
6. **送信予約** はGmailの `in:scheduled` とDB stateの両方を照合。どちらか不明/失敗/矛盾ならUNKNOWN/REVIEWで停止。予約済みをDRAFTやCLEARに戻さず、新しいdraft/scheduleを作らない。
7. **Calendarイベント** は `calendar_event_id` をprovider event IDとして保持し、`(provider, event_id)` uniqueにする。新規作成の論理キーはGAS case ID + appointment cycle/revision（再調整を別予定として記録）とし、医院単位やThread単位で一意にしない。API timeout後はまずID/論理キーを照会し、作成成否不明なら再作成せずUNKNOWNとしてレビューする。現状のDB案はCalendar event ID unique indexのみで、logical idempotency key/outboxは未実装。
8. **Meet URL** は対応するCalendar Event IDに従属させ、別フィールドに保持。Meet URL文字列を予定IDとして使わず、会議変更時はEventのrevision/historyを記録する。
9. **Chat通知** は通知種別 + GAS case ID + appointment revision/status遷移 + Chat space/recipientを論理通知キーとし、送信前にoutbox reservationをDBで一意に確保。Chat provider message IDと結果を保存。送信APIの応答が不明なら再送せずUNKNOWNにして照合する。現在このoutbox/ID記録/通知実装はない。
10. **開封/クリック** はsource provider event IDをsource event keyにし、tokenはcase/operationに結び付ける。イベントIDが取れないときは内容類似で自動マージせずレビュー。IP/User-Agent等は保存を最小化し、生IPを監査ログやキーに含めない。

### UIとPR方針

- Macローカル版の既存サイドバー/画面を完成基準として維持する。次UI変更では既存画面配下の小タブ、またはサイドバーに「営業メール管理」「アポイント管理」を追加する最小変更を優先する。既存のメール送付文章UIを大幅改変しない。
- 次の実装PRは**Read-only GAS/Gmail inventory & reconciliation preview**に限定する。GAS実コード/列契約を確定してから、人工fixtureと提供された読み取り専用exportで source key/row hash/ID mapping/競合結果を出す純粋処理とテストを追加。DB書込、Gmail draft create、GAS registration、Calendar/Chatを含めない。
- 次PR以降は (a)重複判定/状態モデルのみ、(b)追加DBサイドカーとRLS/ACL、(c)最小UI、(d)非本番sync、(e)GAS/Gmail/Calendar/Chat接続の順に分ける。side effect実装PRは別承認後に限定canaryとして扱う。

### テスト実行結果

期待Pythonは依存lock文書が示すPython 3.12。初回のシステムPython 3.9.6では5件失敗した。うち1件は `consultation_schedule.py` の `frozenset | None` 評価で `TypeError` となり、Python 3.9とコード要件の不一致が原因。4件はGmail mock復旧テストが期待した `DRAFT_RECOVERED` を返さず `UNKNOWN` になったが、Python 3.9環境固有の原因は切り分けていない。同一対象をPython 3.12.14 + ClinicLeadComdeskFixの既存pytest環境で再実行すると4件を含め全て通過し、3.9環境の失敗は再現しなかった。リポジトリのソースコードは実行間に変更していないため、3.9の結果は対応ランタイム外での結果として別記し、今回の調査変更起因とは判断しない。

```text
/Users/maekawahiroyuki/ClinicLeadComdeskFix/.venv/bin/python -m pytest -q [関連19テストファイル]
212 passed in 1.59s
```

対象はGAS UUID/operation key/client/transport/local ledger/mock bridge、read-only canary、Gmail workflow/draft/履歴/予約状態、batch、template/generation、repository、CSV import preview、tracking event purity、既存UI read-only isolation。GAS/Gmail/Calendar/Chat/DBへの接続は行わず、fixture/mock中心。テストの一時ファイルはpytest temp領域を使用した。

### 次工程の開始前に必要な確認/承認

1. (Phase 1時点の未完了項目。Phase 2で解消) 4つのGAS参照ファイルを提供し、GASコード/設定のREAD ONLY調査対象として確認する。特に `CaseColumns.gs` とChat通知V2を読む。
2. GAS側の案件ID発番ルール、operation_keyの存在/一意性、送信予約/送信/返信の状態遷移、各Sheet列の唯一の更新元をコードと説明の両方で確認する。
3. 同一医院への別campaign再送を今後どう許可するかを営業運用責任者が決定する。現行コードはclinic-wide guardで二回目を止めるため、解除は既定動作にしない。
4. 次PRの範囲（ローカル純粋関数+mock/人工データだけ）に合意する。GAS/Gmail/Calendar/Chat/Production DBの読み書き、Migration、deploy/mergeは別々の明示承認を必要とする。
5. DBサイドカーを実装する段階ではrole/connection/RLS/ACLおよびMigration履歴のレビュー・承認を行う。今回のローカルテスト実行はProduction DB適用状態や権限を検証していない。

### Production影響 (Phase 1再確認)

Phase 1では本書以外のファイル変更なし。テストはPythonのmock/in-memory/temporary filesystemのみで、Production DB、Migration、Cloud Run、GAS/Sheets/Workers、Gmail作成/送信/予約、Calendar、Chatには到達していない。テストは212件PASS。既存Mac完成基準worktreeは操作していない。

## Phase 3: 重複判定・状態モデル強化

`src/outreach/gas_readonly_inventory.py` を拡張し、インベントリの行は統合せず、`Reconciliation`ごとに理由・関連行・ID種別を返す。

### 重複/関連履歴の分類

| 分類 | 根拠 | 扱い |
|---|---|---|
| `SAME_SOURCE_ROW_REIMPORT` | 同一source_id/tab/source row、row hash一致の別snapshot | 再取込と表示。行は保持 |
| `SAME_EMAIL_CROSS_FORMAT` | Gmail Draft ID、Message ID、operation_keyの完全一致 | 同一メール/操作候補。衝突情報があれば`IDENTIFIER_CONFLICT`も付与。統合しない |
| `SAME_CLINIC_CAMPAIGN_CANDIDATE` | Clinic UUID + campaign_id + recipient_email一致 | 同じcampaignの重複候補。曖昧/複数送信は別行でレビュー |
| `DIFFERENT_CAMPAIGN_HISTORY` | Clinic UUID一致、campaign_id相違 | 別campaign履歴として保持。clinic-wide draft guardは未変更 |
| `SAME_GAS_CASE_REFERENCE` / `SAME_TRACKING_TOKEN_REFERENCE` | 案件ID/追跡トークン一致 | 案件・追跡の関連参照。メール単位の同一性とは扱わない |
| `SAME_THREAD_DIFFERENT_MESSAGE` | Thread ID一致かつMessage ID相違 | 同一会話の別Message（返信を含む）。統合しない |
| `DRAFT_SCHEDULED_CONFLICT` | Sheet下書き記録と検証済みGmail SCHEDULED観測の不一致 | REVIEWし、新規Draftを保留 |
| `IDENTIFIER_CONFLICT` | 同一メール候補のClinic UUID/案件ID/宛先/operation_key矛盾、同じタブの案件ID重複、またはGmailとSheet ID不一致 | REVIEWし、自動紐付けしない |

Thread ID共有だけの場合は関連行と理由だけを示し、重複メール分類を付けない。GAS案件IDやtracking tokenはcase-level参照で、Draft/Message IDと同じ意味にしない。Clinic ID/nameだけの一致はClinic UUID/campaign/宛先を補う証拠がないため、自動分類しない。

### 状態モデルと新規Draft保留

`MailState`: `DRAFT`, `SCHEDULED`, `SENT`, `REPLIED`, `PENDING_REVIEW`, `ERROR`, `UNKNOWN`。Sheet状態は`sheet_status`として原記録に近い形で別保持し、Gmail確認済み状態は、呼出側が明示的に渡した検証済み`GmailObservation`だけから設定する。GASの送信済み/返信/下書き記録はGmailライブ状態に昇格しない。予約状態やMessage IDがGAS原票に存在しない場合、`UNKNOWN/REVIEW`を保つ。

状態遷移は検証済み証拠がない場合`PENDING_REVIEW`へ保留する。許可遷移は`UNKNOWN→DRAFT/SCHEDULED/SENT/ERROR`、`PENDING_REVIEW→各確認状態`、`DRAFT→SCHEDULED/SENT/ERROR`、`SCHEDULED→SENT/ERROR`、`SENT→REPLIED/ERROR`、`ERROR→PENDING_REVIEW`。逆行/不許可遷移はレビュー対象。既存履歴行の`holds_new_draft`は常にtrueとし、Gmail照会なしのUNKNOWN、予約中、下書き中、送信済み、返信済み、競合のいずれも新しい下書き作成を認可しない。実際の送信/作成コードやclinic-wide guardは変更していない。

`source_id`と`revision`は任意メタデータ。複数revisionを比較する場合は同じsource_idを渡す。source_idがない場合、提供データだけで同一原票位置を証明できないため同一行再取込とは断定しない。Gmail観測値は純粋関数への入力であり、テスト/実装からGmail APIへのアクセスは行わない。

### Phase 3検証

人工fixtureでPhase 2の4ケースを後方互換性確認し、再取込、別形式ID一致、case参照、Thread内別Message、同一/別campaign、同一メールIDのClinic UUID競合、単一行のSheet/Gmail予約とDraft競合、同一操作内のDraft/Scheduled並行状態、verified Draftの再作成保留、source_idなしの明示観測、検証証拠不足/状態逆行を確認した。`tests/test_gas_readonly_inventory.py`: **15 passed**。この単一moduleに限定し、外部接続・DB・Migration・Streamlitは使用していない。
