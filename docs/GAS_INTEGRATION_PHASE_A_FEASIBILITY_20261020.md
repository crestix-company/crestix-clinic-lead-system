# Clinic Lead × GAS × Gmail × Sheets 統合：Phase A実現可能性調査

調査日: 2026-10-20 (JST)  
対象ブランチ: `feat/gas-outreach-integration-v2`  
結論: **CONDITIONAL PASS**（オフライン設計・fixture段階まで。Phase Bの実データ照合・外部接続・書込は承認待ち）

## 調査範囲と安全境界

- ローカルのClinic Leadコード、4つのGAS参照ソース、既存設計資料・migration案を静的に確認した。
- 対象スプレッドシートはメタデータと指定6タブの1行目だけを読んだ。医院・宛先・履歴などのデータ行、数式、セルノート、Drive revisionは取得していない。各タブの列数/値型はコードからの推定を含む。
- Gmail API、GAS Web App、Calendar、Chat、Cloudflare Workers、Supabase、Cloud Runには接続していない。
- Phase Aの成果物として本レポートを追加した。GAS/Sheets/アプリコード/migrationは変更していない。GAS参照コードは未変更・未コミット。PR/mergeなし。
- HP自動調査のJob/Controller/Lease、Streamlit、ポート8510、HP関連プロセス、`~/ClinicLeadLocalReadOnly`には触れていない。

## 総合判定

既存GASはフォーム受付からGmail下書き、Calendar/Meet、Chat通知まで実装している一方、Clinic Leadとの共通ロック、GAS→外部の案件登録API、Gmail送信予約/Message ID同期は参照コードにない。Clinic Lead側にはGmail Draft作成、読取照合、Clinic単位の強い重複ガード、GAS用のモック連携クライアントがある。しかしClinic Leadだけの事前照合では、GASと同時に動いた場合の二重下書きを原理的に防げない。

さらに、再実行後のDraft回収を期待する既存Gmail workflow fixture 4件が、本worktreeのPythonで失敗した。RFC 5322の折り返し継続空白がヘッダー値に残り、完全一致照合に失敗していた。後続のローカル修正で折り返し空白だけをunfoldし、キーの完全一致は維持した。元の53件は54件（ヘッダー正規化契約テストを1件追加）となり全件通過。Gmail実機での応答形式は未確認。

よって、**オフラインの照合設計・人工データテストは進められる**。ただし、GASとClinic Leadの双方が同じ医院の下書きを作成できる状態での自動連携は、共通ロックまたは単一作成元を定めるまで開始しない。条件の詳細は本書末尾を参照。

## A-1. GAS 4ファイルの実装調査

「参照コードで実装」「一部実装」「未実装」「実機未確認」を区別する。実機未確認は参照ソースに実装があってもデプロイ版・設定・トリガー稼働を確かめていないことを示す。

| 項目 | 参照コードで確認した実装 | 判定・注意 |
|---|---|---|
| 1. メール下書き作成 | `Index.html` → `submitForm(data)` → `findCandidateSlots_()` / `buildMailBodyText_()` / `buildMailBodyHtml_()` → `GmailApp.createDraft()`。`saveCase_()`とメール管理へDraft ID/Thread ID/日時を保存 | **実装済み・実機未確認**。自動送信処理ではなく下書き作成 |
| 2. 下書き状態管理 | `メール管理`に「下書き作成」「Draft ID」「Draft Thread ID」。`syncMailStatus()`は初期状態を「下書き作成」とし、送信が見つかれば更新 | **一部実装**。現存Draft API照会や明示的DRAFT検証ではない。Sheet値は過去の記録 |
| 3. Gmail送信確認 | `syncMailStatus()`はThread ID、または宛先/件名/過去90日検索を使い、作成時刻-2分以降・件名・自社送信者・宛先で送信Messageを推定。Thread/日時/送信有無を更新 | **一部実装**。Gmail API Message IDは保存しない。検索は候補の同一性を推定するヒューリスティックで、確定ID照合ではない |
| 4. 送信予約確認 | 4ファイル内にGmail Scheduled検索、予約時刻列、予約状態処理がない | **未実装**。予約中を送信済み/下書きと区別できる保証なし |
| 5. Gmail返信検知 | `syncMailStatus()`が送信後の受信MessageをThread内から抽出。`isAutoReply_()`で自動返信を除外し、初回返信日時/自動返信数/返信有無を更新 | **一部実装**。人の返信をSheetに記録するがMessage IDなし。Threadと時刻が根拠 |
| 6. 返信文自動生成 | 返信分類列・受信検知はあるが、返信文の生成・同じThreadに対する返信Draft作成処理なし | **未実装** |
| 7. FS Calendar空き照合 | `getFreeBusyCalendarInfo_()`、`getClinicCandidateIntervals_()`、`findCandidateSlots_()`等がCalendar FreeBusyとFS稼働枠を使用 | **実装済み・実機未確認**。Calendar API設定/共有権限は未確認 |
| 8. 候補日時生成 | `findCandidateSlots_()`、`applySchedulingConstraints_()`、区間の減算/マージ・曜日別診療時間/例外処理 | **実装済み・実機未確認**。候補ログは案件ID/候補番号で追記 |
| 9. 日程調整URL | メール本文には候補日時を表示。`buildTrackingLinks_()`は記事クリック用URLを生成し、`caseId|token|target`のHMAC署名を付ける。選択日時送信用のスケジューリングURLや期限パラメータは見当たらない | **未実装**（記事クリック追跡URLは実装）。追跡トークンはSheetの案件と照合されるが、有効期限はコード上確認できない |
| 10. Calendar Event作成 | `confirmEmailAppointmentManual()`が入力を検証し、FS空きを再確認。`buildManualAppointmentEventId_()`は案件ID由来の決定的ID。`Calendar.Events.get/insert()`で重複確認・作成、409等では同IDを再取得 | **実装済み・一部制約あり**。同一案件内の予定変更サイクルを区別するキーはない。別FS検索の非404エラーをログだけで続行する箇所があり、全カレンダーを確認できない場合の重複余地が残る |
| 11. Meet URL | Calendar insertに`conferenceDataVersion:1`と`hangoutsMeet` createRequestを指定。event IDをrequestIdにし、取得後にMeet URLを抽出。見つからない場合は最大3回再読込 | **実装済み・実機未確認** |
| 12. Google Chat通知 | `sendManualAppointmentChatNotification_()`はWebhookを最大3回試行し、PropertiesServiceのキー(case/date/FS)を成功後保存。V2は1分トリガー、ScriptLock、最大500行走査、成功済みキーとSheet状態を確認 | **実装済み・実機未確認**。Webhook成功後、応答タイムアウト/成功キー保存前の障害は通知二重化しうる。V2はWebhook成功後に`done`を保存し、その後Sheet更新 |
| 13. 開封/クリック計測 | `doPost()`がsecret・event type・case/tokenを検証し、`appendTrackingEvent_()`と案件集計を更新。`buildTrackingLinks_()`はHMAC署名付きclick URLを作成 | **一部実装・Worker実機未確認**。Cloudflare Workerのソースは参照範囲になく、実際の中継/信頼度判定は未検証 |
| 14. 既存Sheet更新 | `submitForm()`がフォーム回答・案件管理・日程候補ログ・メール管理に追記。`syncMailStatus()`がメール/案件状態を更新。追跡POST、Calendar確定、Chat状態、ダッシュボードも更新 | **実装済み**。更新処理はGAS側の副作用。`setupSystem()`は設定値/不足ヘッダー/書式を変更し、`setupCaseColumns()`はバックアップ後に列追加・移動・非表示・トリガー作成する。今回いずれも実行せず |
| 15. 定期トリガー | `createMailSyncTrigger()`は5分ごと。`CaseColumns.gs`は5分更新+onEdit。`EmailAppointmentChatV2.gs`は1分ごと | **コード実装済み・実機未確認**。トリガー所有者/稼働状況は未確認 |
| 16. 再試行・冪等性 | Chat V2はScriptLockとcase+appointment time+FSキー。手動Calendarは決定的event IDと409再読込。メール同期は同じ行を上書き更新。`submitForm()`はcase IDを時刻+3桁乱数で生成し、共通lock/一意制約なし | **部分実装**。メール/外部Gmailに共通operation keyなし。Chat Webhook成功の応答が曖昧な時は同一通知を再送しうる。Calendarのcase単位IDはリスケ周期を表現しない |

### GAS案件/状態IDの役割

- GAS案件ID: `yyyyMMdd-HHmmss-` + 100〜999の乱数。受付ごとのcase IDで、Clinic UUID/clinic_idではない。
- 追跡トークン: `Utilities.getUuid()`からハイフンを除いた値。案件IDに結び付く追跡URL検証用。メールIDや医院IDではない。
- Gmail Draft ID: `GmailApp.createDraft()`返却のDraft resource ID。
- Gmail Thread ID: DraftのMessageから取得する会話ID。同一Threadに複数Messageが存在可能。
- Gmail Message ID: GAS参照コードは保存していない。Clinic Lead Gmail APIはDraft配下Message IDと各既存Message IDを読める設計だが、GAS IDとの自動対応はない。
- Clinic UUID/clinic_id/operation_key: GASの`Code.gs`で生成/保持していない。現Sheetの`v3下書き管理`にUUID/clinic_id列、`ClinicLead_GAS連携`にoperation_key/UUID列があるが、提供4ファイルにその列を更新する関数がない。

## A-2. 既存スプレッドシート列契約

対象は「第一営業部_メール日程調整管理」。Google Sheetsメタデータで6対象タブと列幅を確認し、各タブの1行目だけを読み取った。行データや数式は読んでいない。

「型」は提供コードから読み取れる用途上の型。値の実際の型/書式はセル値を読んでいないため未確認。`Clinic Lead変更`は既存タブへのデータ変更方針を示し、すべて**変更不可**。

### 案件管理（現シート83列）

Sheetのヘッダーは17の表示列、その後に管理列を配置。`Code.gs`の`CASE_HEADERS`宣言順とは異なるが、`saveCase_()`は現ヘッダーを読み、名前ごとに値を配置する。`CaseColumns.gs`もヘッダー名で照合し、`setupCaseColumns()`は列を並べ替える書込処理を持つ。現行ヘッダーは以下（現行順）：

`受付日時`, `IS担当`, `クリニック名`, `宛名`, `メールアドレス`, `電話番号`, `送信日時`, `初回開封推定日時`, `送信から開封までの日数`, `記事クリック有無`, `返信有無`, `初回返信日時`, `開封から返信までの日数`, `返信分類`, `メール経由アポ`, `アポ日時`, `返信から商談までの日数`, `出席`, `有効or無効`, `案件ID`, `宛名役職`, `対応者`, `了承状況`, `記事状況`, `テンプレート`, `掲載記事URL`, `候補①`, `候補②`, `候補③`, `候補④`, `候補⑤`, `希望時間帯`, `有償認識`, `受け入れ`, `人柄の印象`, `記事名`, `懸念点`, `懸念点（詳細）`, `トーク内容`, `トーク内容（詳細）`, `注力科目`, `HP URL`, `その他備考`, `追跡トークン`, `下書きID`, `下書きThreadID`, `下書き作成日時`, `送信有無`, `送信ThreadID`, `画像読込総数`, `送信前画像読込数`, `送信後画像読込数`, `初回画像読込日時`, `最終画像読込日時`, `GoogleProxy読込数`, `AppleProxy読込数`, `Scanner読込数`, `HumanLike読込数`, `高信頼開封`, `開封推定レベル`, `初回高信頼シグナル日時`, `初回クリック日時`, `クリック要求数`, `高信頼クリック数`, `自動返信数`, `担当FS`, `カレンダーEventID`, `Meet URL`, `アポ確定日時`, `Chat通知状態`, `商談出席`, `有効商談`, `受注`, `受注金額`, `月曜診療`, `火曜診療`, `水曜診療`, `木曜診療`, `金曜診療`, `土曜診療`, `日曜診療`, `イレギュラー`, `社内備考`。

| 列群（上の全列名に適用） | 型・意味 | GAS writer / タイミング | 数式・trigger依存 | Clinic Lead変更 |
|---|---|---|---|---|
| `受付日時`, `IS担当`, `クリニック名`, `宛名`, `メールアドレス`, `電話番号`, `案件ID`, `宛名役職`, `対応者`, `了承状況`, `記事状況`, `テンプレート`, `掲載記事URL`, `候補①`〜`候補⑤`, `希望時間帯`, `有償認識`, `受け入れ`, `人柄の印象`, `記事名`, `懸念点`, `懸念点（詳細）`, `トーク内容`, `トーク内容（詳細）`, `注力科目`, `HP URL`, `その他備考`, `月曜診療`〜`日曜診療`, `イレギュラー` | 日時、ID/URL、氏名/連絡先、カテゴリ、選択値、自由記述、診療枠文字列 | `saveCase_()`が`submitForm()`時に追記。受付データはフォーム回答にもJSON保存 | 受付時に値書込み。clinic/campaign UUID列はなし | **変更不可**。Case UUIDは別の照合層で管理 |
| `送信有無`, `送信日時`, `送信ThreadID`, `下書きID`, `下書きThreadID`, `下書き作成日時`, `返信有無`, `初回返信日時`, `自動返信数` | enum/日時/文字列/整数。Gmail状態のSheet観測 | 下書き作成時は`saveCase_()`。送信・返信は5分トリガーの`syncMailStatus()`→`updateCaseMailStatus_()` | CaseColumns onEditが案件ID/送信/返信/アポ列を監視 | **変更不可**。GASはDraft/Thread IDのみ、Message ID/予約状態なし |
| `初回開封推定日時`, `送信から開封までの日数`, `開封から返信までの日数`, `返信から商談までの日数` | 派生日時/整数 | `CaseColumns.gs`の`caseColumnsCalculate_()`と`caseColumnsRefresh_()`が`setValues()` | `refreshCaseLeadTimes()` 5分トリガー+onEdit。**セル数式ではなくスクリプト計算値** | **変更不可** |
| `記事クリック有無`, `初回クリック日時`, `クリック要求数`, `高信頼クリック数`, `画像読込総数`, `送信前画像読込数`, `送信後画像読込数`, `初回画像読込日時`, `最終画像読込日時`, `GoogleProxy読込数`, `AppleProxy読込数`, `Scanner読込数`, `HumanLike読込数`, `高信頼開封`, `開封推定レベル`, `初回高信頼シグナル日時` | enum/日時/整数/分類。追跡イベント集計 | `doPost()`→`appendTrackingEvent_()`→`refreshTrackingSummaryForCase_()`、CaseColumnsのtracking計算 | 外部tracking POST後および5分リフレッシュ | **変更不可**。UserAgent/IP等の生データをClinic Lead側キーにしない |
| `メール経由アポ`, `アポ日時`, `担当FS`, `カレンダーEventID`, `Meet URL`, `アポ確定日時`, `Chat通知状態` | boolean相当/日時/担当名/provider ID/URL/enum | 手動確定の`confirmEmailAppointmentManual()`がCalendar作成・Meet取得・該当列更新。V2/手動Chat関数がChat通知状態更新 | CaseColumnsがアポ日時を監視、5分/onEditでリードタイム再計算。実際のCalendar/Chatはユーザー操作/時間トリガー | **変更不可**。リスケ/再通知は既存処理との衝突がある |
| `出席`, `有効or無効`, `商談出席`, `有効商談`, `受注`, `受注金額`, `返信分類`, `社内備考` | enum/boolean/金額/自由記述 | 4つのGASソース内で一意の自動更新元を確認できない。手入力/外部運用列と推定 | 未確認 | **変更不可**。更新元をSheet所有者に確認 |

### メール管理（13列）

| 列 | 型・意味 | writer / タイミング | Clinic Lead変更 |
|---|---|---|---|
| 案件ID | 文字列外部キー | `submitForm()`追記、`syncMailStatus()`のcase照合 | 不可 |
| 宛先 | メール文字列 | `submitForm()`追記 | 不可 |
| 件名 | 文字列 | `submitForm()`追記、送信検索に使用 | 不可 |
| 下書きID | Gmail Draft resource ID文字列 | `submitForm()`追記 | 不可 |
| 下書きThreadID | Gmail Thread ID文字列 | `submitForm()`追記 | 不可 |
| 下書き作成日時 | 日時 | `submitForm()`追記 | 不可 |
| 状態 | enum（下書き作成/送信済み/返信あり） | `submitForm()`初期値、5分ごと`syncMailStatus()` | 不可 |
| 送信日時 | 日時 | `syncMailStatus()` | 不可 |
| ThreadID | Gmail Thread ID | `syncMailStatus()`。Draft Threadから送信Threadへ同じ列を再利用 | 不可 |
| 返信有無 | enum | `syncMailStatus()` | 不可 |
| 初回返信日時 | 日時 | `syncMailStatus()` | 不可 |
| 自動返信数 | 整数 | `syncMailStatus()` | 不可 |
| 最終同期日時 | 日時 | `syncMailStatus()` | 不可 |

送信Message ID、Scheduled状態、scheduled_at、reply Message IDの列はない。`syncMailStatus()`はこのタブを**固定位置**（A:Iを読み、G:Mを更新）で扱うため列追加/並べ替えは互換性を壊す。

### v3下書き管理（17列）

実シート見出し: `Gmail Draft ID`, `宛先`, `件名`, `Gmail状態（取得時）`, `取込確認日`, `送信状況`, `確認担当`, `備考`, `Clinic UUID`, `clinic_id`, `クリニック名`, `Gmail Message ID`, `Gmail Thread ID`, `Clinic UUID・医院照合状況`, `掲載判定`, `Article Type`, `PR・監修除外判定`。

ID/email/件名/状態/確認日/担当/clinic identity/article判定のsnapshot列。提供4 GASファイルにこのタブの読み書き関数はない。更新主体、更新周期、値の型/数式は**未確認**。clinic uuid/clinic_idは既存の確認情報として尊重する設計だが、重複行を自動統合する根拠にはしない。Clinic Lead変更不可。

### ClinicLead_GAS連携（26列）

実シート見出し: `operation_key`, `clinic_uuid`, `gas_case_id`, `campaign_id`, `recipient_email`, `clinic_name`, `director_name`, `is_staff`, `fs_staff`, `article_status`, `example_article_url`, `listing_url`, `gmail_draft_id`, `gmail_message_id`, `gmail_thread_id`, `tracking_token`, `sheet_sync_status`, `gmail_sync_status`, `appointment_status`, `sync_attempt_count`, `last_synced_at`, `source_updated_at`, `record_created_at`, `last_error_code`, `review_status`, `schema_version`。

文字列/ID/日時/状態/再試行回数/版数の連携契約と推定。`Code.gs`の`APP.SHEETS`にもこのタブがなく、4ファイルに読み書きする関数はない。Python `GasCaseRegistrationRequest`は既存Draftを含むpayloadを作るが、このGASスナップショットへ保存する機能とは確認できない。既存の橋渡しローカル台帳は`operation_key`等をJSONLに記録するのみ。本タブの実際のwriter・trigger・数式・データ行数は今回未確認。Clinic Lead変更不可。

### 日程候補ログ（7列）

実シート見出し: `案件ID`, `候補番号`, `日付`, `開始`, `終了`, `表示文字列`, `対応可能FS`。`saveCandidateLog_()`が`submitForm()`時に候補行をappend。案件IDは多数行で共有される親case keyであり、繰返しを重複と判定しない。候補番号は整数、日付/開始/終了は日/時刻表現、残りは表示/担当文字列とコードから推定。数式・定期更新なしをコード範囲で確認。Clinic Lead変更不可。

### トラッキングログ（16列）

実シート見出し: `受信日時`, `案件ID`, `イベント`, `分類`, `信頼度`, `UserAgent`, `IPハッシュ`, `ASN`, `ネットワーク組織`, `国`, `Sec-Fetch-Dest`, `Sec-Fetch-Mode`, `Accept`, `Referer`, `CF-Ray`, `追跡トークン`。`doPost()`で検証後`appendTrackingEvent_()`が追記し、`refreshTrackingSummaryForCase_()`が案件集計を更新する。イベントID/API source event keyの安定した固有列は見当たらず、行再取込はGmailメッセージ重複と別に扱う。UserAgent/Referer/CF-Ray/追跡識別子を医院一致キーにしない。Clinic Lead変更不可。

## A-3. UUID紐付け契約と精度検証

### 現状の実装契約

`src/outreach/gas_sheet_bridge.py:resolve_clinic()`はClinic UUID形式を検証し、医院名正規化後に、**医院名 + 完全電話一致または完全住所一致**、他の与えられた電話/住所/院長/メールに矛盾がなく、候補が一意の場合だけMATCHEDを返す。医院名のみ/メールのみ/曖昧候補/矛盾はREVIEW。`plan_registration()`は履歴未検証ならBLOCKED、既存operation_keyならREVIEW。

不足: `LegacyCase`に既存確認済み`clinic_uuid`や既存`clinic_id`がなく、確認済みUUIDを保持して優先する入力契約がない。`clinic_id`は`ClinicIdentity`/resolverにもない。既存のv3 tabからUUIDを受け取る場合に再判定で上書きせず保持する層が必要。GAS本体にはUUID照合/生成がない。

### 人工fixture検証計画

1. 既存確認済みUUID完全一致: 既存値をimmutableな確定値として保持し、候補側の競合はREVIEW。
2. 医院名+電話完全一致の一意候補、医院名+住所完全一致の一意候補。
3. Clinic UUID不正、医院名なし、候補なし、同名/同電話複数候補。
4. 電話/住所/院長名/メールのどれかに明示矛盾、別住所への移転、同名別院。
5. 医院名のみ、メールのみ、電話のみ、住所のみは自動確定しない。
6. 既存inner ID/clinic_idがClinic Masterで一意にClinic UUIDへ解決、未解決/多重解決。
7. 複数GAS案件が同じClinic UUIDへ対応する場合もcaseを維持し、clinicに集約して消さない。

評価: MATCHEDのprecisionを100%要求（fixture全て正解）、coverageは別計測して精度と混同しない。曖昧群は全てREVIEW。実clinic master/送信履歴は読んでおらず、実データ精度や候補数の統計は**未検証**。実データ照合は読取範囲・出力マスキング・承認を別途確定後に実施。

## A-4. Gmail ID/状態検証

| 概念 | 意味・利用ルール |
|---|---|
| Draft ID | Gmail API Draft resource ID。GAS `getId()`の値。Draftが送信/削除された後も同IDで現在状態にアクセスできるとは仮定しない |
| Gmail Message ID | Gmail API `message.id`。Draft内部のmessage IDと送信後Message IDはDraft IDと別フィールド。RFC `Message-ID` headerとも区別する |
| Thread ID | 会話スレッドID。1 Threadに送信Messageと複数返信を保持しうる。重複排除キーにしない |
| Scheduled | GAS管理列なし。Clinic Lead `read_gmail_state()`は`in:scheduled` queryを使うが、実アカウント上でqueryの網羅性/解除・削除後挙動を検証していない。予約取消/再予約操作は本スコープで提供しない |
| Sent | Gmail API metadata (Message ID, threadId, To, Subject, Date, Clinic/operation custom headers等)で確認する設計。送信履歴は存在確認と同一医院/操作の照合を分ける |
| Reply | 同じThreadのMessage一覧から、送信後・自社送信者以外のMessageを候補とする。Threadだけではどの送信への返信かを確定せず、Message ID・日時・recipient/clinic/outreach identityを照合 |
| Trash/deleted | Clinic Lead readerは`in:trash`も別状態として取得し、TRASHEDをCLEARにしない。GAS `syncMailStatus()`はTrash状態を区別しない。Gmail Trashから消えた履歴は独立台帳がなければ証拠が失われる |
| Query/API failure | `read_gmail_state()`はどれか一つのlist/get queryが失敗すればworkflowがREVIEWで停止する設計。GAS側同期はThread取得失敗後に検索するが、検索例外/途中停止の再開キューは見当たらない |

GASからの旧履歴復旧案: ①SheetのDraft IDが現存する場合に限り、Gmail read-only query/getでそのDraft resourceとheader metadataを正確に確認。②Draftがない場合は送信候補を宛先/件名/作成時間/自社送信者/thread/custom IDsで検索し、Message IDまで特定できない、候補複数、日時の不整合はREVIEW。③tracking token、Thread ID、宛先/件名だけからMessage IDを作らない。復旧照会結果を元タブへ書き戻さず、先にlocal previewへ出す。

Clinic Lead Gmail設定は`gmail.readonly` + `gmail.compose`で、送信専用scopeではないもののcompose scopeはdraft管理以上の能力を持つ。既存feature flagは既定OFFで、Production OAuthを今回使用していない。future state syncはReader専用最小scope/アカウント分離可否をOAuth管理者と確認する。

## A-5. 重複・競合ケースと検証

| ケース | 既存構成の対策/限界 | 合格動作 |
|---|---|---|
| GASでDraft済み→Clinic Lead作成 | Gmail metadata照会+Clinic identity heuristicで検出可能。ただしGAS custom idempotency headerとoperation_keyなし、Clinic UUID対応なしの旧Draftは候補一致止まり | exact Gmail ID/verified identityを優先。曖昧一致はREVIEWし作成しない |
| Clinic Lead Draft済み→GAS受付 | Clinic Lead DB guardはGASから見えず、`Code.gs`は同DB/operation keyを確認しない | **現状は相互排他不可**。単一作成元/共通ロックがない限りClinic Lead自動作成を同じclinicで許可しない |
| GAS/Clinic Leadの同時作成 | DB clinic-wide guardはClinic Lead内の同時キャンペーンを直列化するがGASには適用されない。Sheet/Gmail preflightはTOCTOU | GAS無変更のまま二重作成を保証付きで防ぐ方法なし。共通のGAS確認/ロックまたは人間の運用排他が必要 |
| 同一医院・同一宛先・同一campaign | UUID+campaign+recipient+template operation keyで候補化。宛先/件名のみは弱い証拠 | 既存送信/予約/下書き、同一operationはREVIEW/BLOCK。別case/正当な再送は削除/統合しない |
| 同一医院・別campaign | 既存`clinic_draft_guard`と`BLOCKING_STATUSES`がDRAFT/SCHEDULED/SENT/REPLIED/TRASHED/UNKNOWN/REVIEW/FAILEDをclinic単位でblock。migration案にも`unique(clinic_id,campaign_id)` | **guardを緩和しない**。履歴は別caseに保持しても新規Draftは既存guardの判断に従う |
| 同じ原票/Gmail履歴の再同期 | Phase 3 source batch checksum/revision/tab/row/hashとGmail Draft/Message IDが個別キー | provider IDまたはsource row keyが一致する観測のみ同一実体候補。source raw row/イベントを削除しない |
| Gmail Draft削除後の再実行 | Clinic LeadはTRASHED/UNKNOWNを保留。GASは削除/Trashを区別できず、Sheet「下書き作成」が残る | 削除をCLEARにしない。Gmail/ledger不一致はREVIEW。自動再作成しない |
| Scheduled→Sent | Clinic Leadが別stateとして読取、GASには予約状態なし | state eventをappendし、Gmail observationごとにscheduled_at/sent_atを保持。予約取消/再予約は別イベント |
| 同じThread内の複数返信 | GASは初回人返信時刻とautoReply countのみ。Clinic Lead既存モデルはThread内複数Messageを列挙可能 | 各Message IDを別履歴として保持し、Thread共有を統合根拠にしない |
| Sheet途中更新失敗 | `syncMailStatus()`は行ごとに更新し、cursor/checkpoint/error tableなし。Chat V2は送信成功後にSheet更新 | Source row/batch cursorとper-row retry/errorを追加設計。Sheet既存列は同期先にせずraw保持 |
| Supabase保存成功・Sheet更新失敗 | proposed `sync_jobs`/`sync_errors`はあるが2系統transactionではない。既存GASはSheet source | append-only DBイベントを正とし、Sheet projectionはoutbox/再試行。Sheetへ上書きしない |
| 外部API timeout | DraftはClinic Leadにexact custom-header再取得機構あり。Calendarは決定的event IDでGET/409回収。Chat Webhookは3回再送で二重通知余地 | timeout時は同じoperation/event keyをGET/検索して結果照合。不明ならUNKNOWNで停止。Chatはoutbox/idempotency keyを持つprovider capability確認 |
| GAS triggerと更新競合 | `syncMailStatus()`はScriptLock未確認、Chat V2はScriptLockあり。Sheet更新の共通CAS/revisionなし | 既存Sheet直接更新をClinic Lead側でしない。ソースsnapshot差分を人が確認し、同一列への二writerを置かない |

### Clinic Lead側既存ガード

- `outreach.clinic_draft_guard`のunique `clinic_id`を`reserve_draft()`がDB transaction内で確保。clinic-wide、campaignをまたいで一度だけを維持する。
- Delivery tableは`unique(clinic_id,campaign_id)`、idempotency key、Draft ID、Message IDを個別unique。Trashもblocking state。
- `create_gmail_draft()`はDraft/API照合失敗でREVIEWし、Draft ID/custom keyで不確実なcreateの回収を試みる。送信/予約のAPI呼出しなし。
- GASの`Code.gs`はこのguard/DBを読まず、Clinic Leadの同時登録にも関知しない。したがって、このガードを「システム全体のlock」と解釈しない。

## A-6. 再利用方法の比較

| 方式 | 互換性 | 安全性/権限 | 保守/コスト | 結論 |
|---|---|---|---|---|
| 既存GASをAPIとして呼ぶ | 現GAS `doGet()`はフォーム(アクセスkey)、`doPost()`はsecret認証付きtracking eventのみ。外部からのcase registration/read/schedule APIなし。別repoのbridge client contractは現在の4 GASファイルと一致する実装/デプロイが確認できない | 既存GASの権限でSheets/Gmail/Calendar/Chatを行うと権限集中。外部APIやoperation key受け入れ実装を追加しない限り利用不可 | GASとClinic Leadのロジック一元化は利点だが、現在のAPIは存在しない | **現時点で呼び出し再利用不可**。本番Web App URL/デプロイ/contract確認が必要 |
| Google Sheets/Gmail API read-only | 既存運用を変更せず履歴snapshot取得が可能。現コード列の意味を保てる | OAuth/service accountの最小read-only scope、PIIマスキング、監査/retentionが必要。Gmail query/metadataは実アカウント検証要 | API依存/ページング・権限管理の実装が必要。同期コスト小〜中 | **過去履歴の最初の連携に推奨**。まず手動export/local preview、次に承認済みread-only reader |
| Clinic Leadに同等機能を実装 | 新規下書き/返信/候補/Calendar機能は一部再利用可能だが、GASのフォーム・候補・運用列・Cloudflare trackingと挙動差 | 独立Gmail/Calendar/Chat権限が必要。Clinic Lead DB lockだけではGASとの競合解消不可 | 並行実装/仕様乖離・二重運用コストが高い | 新規Clinic Lead専用UI/返信Draftには適するが、GAS機能を無確認で置き換えない。共通作成権限は未解決 |

推奨は**ハイブリッドread-only**: 既存GAS/Sheetを過去案件・GAS由来イベントのsourceとし、Gmail実状態はメタデータのみ照合。clinic_uuid候補、source row、provider IDsと証拠をClinic Leadのpreviewに表示。書込/送信は、競合ロックと作成元を別途決めるまで無効。

## 既存連携タブの再利用と新規統合タブ

| 案 | 長所 | 不足/制約 | 推奨用途 |
|---|---|---|---|
| `ClinicLead_GAS連携`を使う | 既存26列のoperation/case/clinic/Gmail/tracking/sync/review契約。名前上、連携用 | 提供GASは未参照・未更新。過去metadata auditではdata rows 0だったが、今回は行を再読していない。source revision/row key、複数message履歴、state event historyがない | ownerがwriter/所有権/列固定を確認できれば**integration projection**として再利用を優先。canonical ledgerにはしない |
| `v3下書き管理`を拡張・再利用 | Draft/clinic UUID/clinic_id/Article等の既存列がある | 更新元不明、operation/campaign/GAS case/tracking/source row/status history/返信Message IDが不足。既存列追加/並べ替えはしない | 読取のみの旧Draft候補ソース |
| 新規「営業履歴統合」タブ | 既存GAS管理タブと責任を分けやすい | Workbookに新しい共有状態を追加。既存GASが無視する保証とowner承認が必要。Gmail状態履歴の正規DBにはならない | Bでsheet projectionが業務要件なら追加承認後に検討 |
| Supabase append-only台帳 + Sheet read-only | 複数source/case/message/eventとrevisionを保持できる。CL内で履歴検索可 | schema改訂/role/RLS/ACL/同期worker/migration実施承認が別途必要。既存migration案は未適用を確認していない | **将来のcanonical record store候補**。実データ照合/DB role確認前に適用しない |

## Supabase保存案のレビュー

`supabase/migrations/20261019160000_outreach_full_sheets_gmail_integration.sql`は提案migrationであり、この調査で接続/適用していない。既存outreach migrationにRLS下の専用`clinic_outreach_runtime` role、Draft/message ID unique、clinic/campaign unique、clinic-wide guardがある。後続migration案には次の追加表がある: `sales_cases`, `gmail_messages`, `email_state_history`, `engagement_events`, `calendar_appointments`, `source_import_batches`, `source_import_rows`, `sync_jobs`, `sync_errors`, `audit_events`等。

### 再利用可能

- `email_delivery_records`: Clinic Lead既存Draft予約・状態/ID。別のoutreach用途にも適用可能だが、clinic/campaign唯一性とclinic-wide guardはそのまま維持。
- `sales_cases`: Clinic UUID/clinic_id、operation_key、source system/external case IDをcase-levelで関連付ける案。
- `source_import_batches` + `source_import_rows`: spreadsheet/tab/revision/checksum・原票行・mapping statusのraw staging。
- `email_state_history`, `engagement_events`, `sync_jobs`, `sync_errors`, `audit_events`: state/event provenance・error recoveryの基礎案。
- `calendar_appointments`: external event ID unique。

### B着手前にschema案を修正・検証する点

1. `gmail_messages.operation_key UNIQUE`はoperation単位に一行しか許さない。一Thread内の送信+複数返信/返信DraftをMessage単位で残すには、`gmail_message_id UNIQUE`のmessage/event子表（operation_keyは非unique group key）か別の`gmail_message_events`を要する。
2. `sales_cases.operation_key UNIQUE`と現行`registration_key()`（Clinic UUID+campaign+recipient+template kind）は、同じcampaign内の意図的な複数case/再送を同一操作としてしまう。intentional new touch/cycle IDを人の承認下で別caseにする契約がない。既存guardは別途blockし、緩和しない。
3. `email_delivery_records`の`unique(clinic_id,campaign_id)`は同clinic/campaignで複数deliveryを保存できない。既存運用制約として変えず、GASの過去メール履歴は新しいmessage-levelテーブルにraw importする。clinic-wide guardが新規draftを阻止する責務と、履歴保存が複数行を保持する責務を分ける。
4. `calendar_appointments`はprovider/external_event_idのみunique。event create timeout前にevent IDを失ったケースやリスケcycleを防ぐlogical `appointment_key` uniqueがない。case+appointment cycle/revisionの追加案を検討。
5. Calendar/Chatのnotification outboxがない。`notification_outbox`に決定的idempotency key、provider、状態/attempt/lease/resultを追加し、Chat webhook retry/timeout二重投稿をレビュー。
6. `sync_jobs`はcursor、`sync_errors`はrow単位失敗を持つが、DB commit後のSheet projectionを確実に再試行するoutbox/leaseは別途必要。
7. `source_import_rows.raw_values`を順序付き文字列として保持、raw hash/source row identityを維持。PII監査/保持期限/閲覧roleをmigration前に定義。
8. 当該migrationファイルには既存delivery recordsからsales_casesへ移すINSERTとUPDATEも含む。DDLだけのmigrationではなく、Productionでは承認済みbackfill/rollback・権限/sequence安全性をレビューして初めて候補となる。

## 実装タスク・PR分割案

このレポートで合格した**オフラインの範囲だけ**が次工程候補。以下は将来案で、今は実施しない。

1. **B0 オフラインimport/UUID mapping preview**: approved export/人工fixture、confirmed UUID保持、raw row hash/source key、field evidence/review reason。no DB/no API.
2. **B1 Gmail message/state model修正**: まず上記Draft header normalizationテスト失敗を別修正。Draft/Scheduled/Sent/Reply/Trash/Unknown/Conflictとmessage-level eventsをmockで検証。送信/予約なし。
3. **B2 Sheets/Gmail read-only connector**: read-only identity/scope、pagination/cursor、row errors、last verified、secrets/log redaction、owner-approved allowlist。開発/テストはmock。
4. **B3 Schema review + separate migration PR**: B0/B1契約に合わせ子message/event/outbox、RLS/ACL、role、rollback/backup、existing unique guard検証。Migration実行には別承認。
5. **B4 Offline UI preview**: 既存UIの小タブ/画面内表示、feature flag OFF、read-only・PIIマスク。Streamlitの本番/HP稼働環境起動はしない。
6. **B5 非本番のGAS/Gmail read-only canary**: external data access approval後。正確なSheet/GAS deployment/version、Gmail query/IDs/Trash, source reconciliationを限定検証。
7. **B6 Dual-writer arbitration / Draft registration**: GAS無変更のまま安全な共通lockがない場合は実施不可。GAS bridgeを追加するか新規outbound唯一の作成元を合意する別仕様・承認が必要。clinic-wide guardは維持。
8. **B7 Reply draft / Calendar/Meet/Chat**: message/thread parentage、appointment cycle idempotency、outbox、sandbox OAuth/Calendar/Chatで順次検証。外部副作用ごとに別承認。

## テスト・受入基準

### 今回の限定fixtureテスト

`python3 -m pytest -q tests/test_gas_sheet_bridge.py tests/test_gas_readonly_inventory.py tests/test_outreach_gmail_workflow.py`

- 初回: 49 passed, 4 failed（計53）。失敗原因を修正後、ヘッダー契約テスト1件を追加し54 passed（計54）。全テストはClinic UUID/Gmail repository/serviceの人工fixtureで、Production DB/Gmail/Sheets/GASへの通信なし。
- Phase 3 inventory 15件とClinic resolver 7件はpass。
- Gmail workflowの4失敗: `test_same_campaign_rerun_recovers_existing_draft_without_second_create`, `test_ambiguous_gmail_create_reconciles_exact_draft_key_once`, `test_db_update_failure_after_gmail_create_then_retry_recovers_without_duplicate`, `test_repeat_same_campaign_is_blocked_by_existing_ledger`。MIME header foldingの継続空白を`src/outreach/gmail_drafts.py::_headers`でunfold/trimし解消。キー自体は完全一致のまま。`test_folded_idempotency_header_is_unfolded_but_still_compared_exactly`を追加。

### ローカル修正後の回帰結果

- 対象54件: `python3 -m pytest -q tests/test_gas_sheet_bridge.py tests/test_gas_readonly_inventory.py tests/test_outreach_gmail_workflow.py` → **54 passed**。
- 関連回帰: 上記にGAS bridge client/transport/ledger/mock/read-only canary、Gmail draft create/history/backfill、delivery repository/history workflowを加えた12ファイル → **141 passed**。
- 対象コードはGmail MIME metadataのローカルparseとmockのみ。Gmail、Sheets、GAS、Supabase、本番には接続していない。

### 次に満たすべきfixture受入基準

- Clinic UUID: exact existing UUID維持、phone/address一意match、name/email-only拒否、複数・矛盾・移転・inner ID候補異常はREVIEW。
- Source: 同一revision/tab/row/hash再取込冪等、revision違いのrow変更検出、数字ID/日時/JSONを文字列のまま保持。
- Gmail: Draft ID/Message ID/Thread IDを個別unique。Thread shared + distinct Message IDsを別行維持。RFC Message-ID headerとAPI `id`を別々に試験。
- State: Gmail metadata live/evidenceなし、scheduled query failure、trash/deleted, sent, multiple reply, auto reply, same thread unrelated message, ambiguous header identityはUNKNOWN/REVIEW。Scheduled→Sentは別イベントとして追加。
- Concurrency: GAS/GAS trigger overlap、Clinic/Clinic guard、GAS/Clinic raceはmock barrierで再現。跨システム同時作成はshared lockなしならfail/holdが合格。
- Recovery: Gmail作成応答timeout、DB commit/Sheet projection failure、途中row retry、Calendar 409/timeout、Chat webhook timeoutの各caseでIDとidempotency keyを保ち、曖昧結果を再作成しない。
- Sheet contract: 既存各tabはheader/body/formula unchanged。書込み対象は承認された新規専用projectorだけ。schema change/column reorderは禁止。

## 合格ゲート

| ゲート | 判定 | 根拠/残条件 |
|---|---|---|
| 既存GASを変えず履歴を読む方法 | **CONDITIONAL PASS** | approved exportまたはSheets/Gmail read-only APIは可能。実scope/production identity未承認 |
| Clinic UUIDの安全な照合 | **CONDITIONAL PASS** | resolverは保守的なunit契約あり。既存confirmed UUID/clinic_idの第一級入力が不足し、実データprecision未測定 |
| Sheetと新規統合タブ責任分担 | **CONDITIONAL PASS** | 6既存タブをread-only sourceとする。`ClinicLead_GAS連携`は候補のprojection contractだがwriter/owner未確認。新規tabも別承認 |
| Gmail状態を誤確定しない | **CONDITIONAL PASS** | Clinic Leadはquery失敗でREVIEW、GASは送信ヒューリスティックで予約/Message ID未保持。4 mock recovery failures修正とaccount-level validationが必要 |
| 同一メールの二重取込防止 | **CONDITIONAL PASS** | source key/ID契約はある。旧GASにoperation keyなし、Draft header recovery mock 4件failure |
| 更新失敗からの復旧 | **CONDITIONAL PASS** | Gmail/DB recovery pattern・sync job/error proposalはあるが、Sheets outbox/checkpoint・Chat outbox不足 |
| Calendar/Meet/通知二重生成 | **CONDITIONAL PASS** | Calendar deterministic ID/409、Chat keyあり。reschedule cycle・cross-calendar query error・ambiguous webhook timeoutの対策不足 |
| テスト可能なインターフェース | **PASS (offline only)** | pure inventory/resolver, Gmail fake services, repo fake, GAS mock clientがある |
| GAS無変更の制約 | **CONDITIONAL PASS** | 読取・過去履歴統合は進められる。二つの作成主体を同時稼働する二重防止保証は不能 |
| Production impact | **PASS** | 今回はローカル静的調査/人工fixtureのみ。外部書込なし |

## 実装前に必要な承認/確認

1. **実データ読取**: 対象6タブのdata rowを読み、UUID候補/重複を作る対象範囲、PII出力/保存期限、Gmail mailboxとread scopeを別途承認。
2. **現行GAS版/列owner**: 提供4ファイルが実稼働deploymentと同一か、Spreadsheet ID/Script ID/版、trigger owner、`v3下書き管理`/`ClinicLead_GAS連携`のwriterと更新責任者を確認。
3. **二重作成を防ぐ責任者合意**: GASとClinic Leadのどちらを新規作成の唯一authorityにするか。GASを維持したまま双方が作る場合、共通lockをどこに持つか。Clinic-wide guardは緩和しない。
4. **Gmail identity/scope**: Gmail API `in:scheduled` queryをテストmailboxで検証。Draft delete/send後のIDの振る舞い、返信Message metadata、Trash retention、custom header normalizeを確認。
5. **既存tab接続**: `ClinicLead_GAS連携`をprojectionに使うならheader/row ownership、single writer、status vocabulary、append/update方式をSpreadsheet ownerが承認。既存列は変えない。
6. **Schema/Production**: migration proposed codeに含まれる backfill DML、clinic-wide guard、unique constraints、role/RLS/ACL、rollback/recoveryの個別review。適用許可が出るまでProduction DBへ触れない。
7. **返信/Calendar/Chat**: reply Draft only/no-send方針、appointment reschedule key、Meet event state、Chat notification outbox/retry policyを承認。

承認前に行わない: 実データ照合、Gmail/GAS/Calendar/Chat/Worker API、Sheet書込、DB/migration、Cloud Run deploy、GAS参照ファイルの変更/commit、PR merge。
