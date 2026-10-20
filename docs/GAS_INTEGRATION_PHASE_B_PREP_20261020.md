# Clinic Lead × GAS 統合 Phase B 実装準備

更新日: 2026-10-20 (JST)  
状態: ローカルfixture実装・設計。外部接続、実データ照合、書込、migration適用は未実施。

## 1. Phase Aの条件付き項目

| 項目 | 今回の結論 | Phase Bゲート |
|---|---|---|
| Draft回収の4 test failure | RFC 5322 header folding whitespaceをunfold/trimして解消。冪等性キーの完全一致は維持 | 実Gmail metadataでも値が同じになることを非本番mailboxでread-only確認 |
| Clinic Lead側の同時作成 | DBのclinic-wide reservation/guardを引き続き必須とする | concurrent fixtureとrepository unique guardを維持。guard緩和は禁止 |
| GASとの同時作成 | GASとClinic Lead間に共有lockがなく、GAS無変更で完全排他は不可能 | 下記のA方式（単一作成元）を先に承認。B方式にはGAS変更が必要 |
| UUID付与 | 一意・矛盾なしの証拠がある候補だけ自動候補化。確認済みUUIDは入力の強い根拠としてそのまま保持 | 実データはread-only照合後、人が曖昧候補を確定 |
| Sheets投影 | 既存の業務タブはimmutable source。新規専用タブへの差分previewから始める | Spreadsheet ownerによるタブ/列/writer契約の承認 |

## 2. 二重下書きの排他方式

### 比較

| 方式 | GAS無変更で可能 | 排他保証 | 評価 |
|---|---:|---:|---|
| A. 作成元をClinic LeadまたはGASに一本化 | 可能 | 一本化した経路の全writerが従う範囲で可能 | **推奨**。Clinic Lead作成を有効化する期間は、GASの同一営業対象フォーム/トリガーからのDraft作成を運用上停止するか、GASを旧案件専用とする。GAS稼働とClinic作成を併走させるなら重複保証はない |
| B. 両経路で共有lock | 不可 | 両方が同じ原子的reservationを取得するなら可能 | GASの`submitForm()`直前に同じoperation keyを使ったshared reservation API/DBを導入し、作成後に結果登録する変更が必要。PropertiesServiceのローカルScriptLockだけではClinic Leadとの排他にならない |
| C. Clinic Lead側でGAS履歴を確認・保留 | 可能 | **保証なし**。確認直後にGASが作成するTOCTOU raceが残る | 新規作成前の安全弁として有効。GAS Sheet/Gmailをread-onlyで走査し一致/曖昧/未照合をHOLDにする。確認窓の短縮はできても同時実行を防げない |

GASを変更しない条件では、Aを作成許可の前提にし、Cを追加防御として使う。Aの運用合意なしにClinic Lead Draft作成を並行稼働させない。共通lock方式へ進む場合はGASコード・デプロイ・設定の別承認が必要。

## 3. UUIDの紐付けと履歴保持

1. 既存`Clinic UUID`/`clinic_uuid`と`clinic_id`の確認済み値は候補再計算で上書きしない。入力値と確認状態をsource evidenceとして保持する。
2. GAS `案件ID`、operation key、Gmail Draft/Message/Thread IDは各々別属性。Thread ID単独では同一送信としない。
3. 同じ医院に複数行/複数送信がある場合、各原票行とMessage IDを維持する。UUIDの一致は行の統合・削除の根拠にしない。
4. 自動確定は、既存の確認済みUUIDと矛盾せず、強い明示ID完全一致または名前+電話/住所の一意一致で、他の提示属性と矛盾しない場合のみ。
5. 既存UUID同士の不一致、医院名だけ/メールだけ、候補複数、移転・改称・住所/電話不一致、ID重複は`REVIEW`。根拠と候補を提示し、人が解決する。
6. Gmail ID完全一致、GAS case ID完全一致、既存確認済みUUID、clinic_id、operation key、名前+住所/電話の順に証拠を記録する。弱い証拠で強い既存値を置換しない。

実シートのdata rowを読んでいないため、この文書は行単位の実照合結果ではなく判定規則である。

## 4. Sheetsタブと同期preview契約

既存`案件管理`、`メール管理`、`v3下書き管理`、候補ログ、管理タブの列/行は読み書きしない。`ClinicLead_GAS連携`はoperation keyとGAS/Gmail ID列を持つためschema上は再利用候補だが、今回提供されたGASコードにwriterがなく、実際のwriter/owner/triggerも未確認。責任主体の確認までは書き先として扱わない。

安全な初期案は、新規の`営業履歴統合`タブを作り、専用projectorだけが書くこと。初期段階はpreviewのみ。既存業務タブから来た事実は原票スナップショットとして保持し、projectorが必要な派生列を出す。タブ作成自体も今回の範囲外で承認後。

同期previewは各source rowごとに、`source_id/tab/row_number/revision/checksum/row_hash`、確認済みClinic UUID、GAS case ID、campaign/operation key、Draft ID、Message ID、Thread ID、Sheet観測状態、Gmail観測状態/evidence時刻、Disposition、差分列、理由を返す。PIIはpreview画面以外のログ/例外に含めない。

| Preview action | 条件 |
|---|---|
| `INSERT` | 対象統合キーに既存投影がなく、UUID/ID/stateが一意・矛盾なし、ソース履歴確認済み |
| `UPDATE` | 同じ投影キーが1行だけ存在し、更新可能フィールドに新しい確認済み値がある。UUIDや以前の確認状態を弱い根拠で置き換えない |
| `NOOP` | source row/hashと投影内容が一致、または新しい確認済み情報がない |
| `HOLD` | 読取未完了、Gmail/Sources不明、複数候補、ID/状態矛盾、複数既存投影、既存確認済みUUIDとの衝突、別履歴を上書きする可能性 |

同じ医院の別campaign/別Draft/別Messageは異なる履歴行。ソース行キーによる再取込冪等性と業務上の送信重複判定を分離する。`HOLD`は新規Draft作成禁止を含む。

## 5. Supabase論理保存モデル

既存delivery ledgerはDraft予約とclinic/campaign単位ガードを継続担当。完全な営業履歴は追加サイドカーで表し、既存テーブルの意味を変えない。

| Entity | 主な属性と制約 |
|---|---|
| `outreach_source_batches` | source system, source id, tab, revision, checksum, captured_at, import status。1 snapshotの読取/再開単位 |
| `outreach_source_rows` | batch FK, source row number, ordered header/value JSON, row hash, preserved raw string values。`(batch_id, tab, row_number)` unique。再importで別revisionを消さない |
| `outreach_identity_links` | source row FK, clinic UUID/clinic_id, decision, evidence JSON, verified_by/at, review status。複数履歴行が同じclinicへリンク可能。確認済みlinkの自動置換禁止 |
| `outreach_operations` | operation key, clinic UUID, campaign, normalized recipient, template kind, current workflow status。Draft作成権限/予約は既存clinic-wide guardと一体で処理 |
| `outreach_messages` | operation FK nullable for legacy, provider, Draft ID, Gmail API Message ID, RFC Message-ID, Thread ID, direction, lifecycle state, observed_at, sent/scheduled timestamps。Draft IDとMessage IDは別のunique constraint、Threadはnon-unique |
| `outreach_message_events` | message FK, event type/state, provider event ID, observed_at, evidence source, payload-safe metadata。`SCHEDULED`, `SENT`, `REPLIED`, `TRASHED`等を追記し状態履歴を保持 |
| `outreach_appointments` | operation FK, appointment cycle key, Calendar event ID, start/end, timezone, FS, status, Meet URL, created/observed timestamps。リスケは新cycleとして保持 |
| `outreach_sync_jobs` | source batch, cursor/checkpoint, attempt, status, started/finished, safe error code。再開可能。PIIをerror textに入れない |
| `outreach_sync_decisions` | source row, action, before/after hashes, reasons, reviewer/time, applied state。previewとapply監査を分ける |

raw snapshotは不変、正規化済みidentityと外部観測は別entityに保存する。RLS/ACL、専用role、PII保持期間、unique scope、既存migration状態はschema PRでreviewするまで未確定。提案migrationの適用状況もProduction接続なしでは断定しない。

## 6. 返信・Calendar・Meet再利用範囲

| 機能 | 既存GAS参照コード | 統合上の判断 |
|---|---|---|
| 返信受信検知 | Thread内の受信messageを調べ、自動返信判定、初回返信日時/件数をSheetへ更新 | 参考実装として再利用可能。Message IDの永続化、Clinic UUIDへの安全なlinkは追加必要。参照コードのGAS実機稼働は未確認 |
| 返信文自動生成/返信Draft | 該当処理なし | 未実装。返信案生成とGmail draft creationを別段階にし、送信は対象外とする |
| 日程候補 | FS診療枠/例外とCalendar FreeBusyから候補生成 | GASロジックを参照可能。営業履歴へのcase/UUID/campaign linkが必要。Calendar API接続は別承認 |
| 調整URL | 記事クリック追跡URLのみ。候補日時選択用URLなし | 未実装。token expiry、署名、one-time booking、変更/取消の仕様が必要 |
| Calendar Event | 手動確定関数がdeterministic event ID、再取得/409処理、空き再確認を実装 | 再利用可能な考え方。reschedule cycle keyと全calendar照会失敗時のfail-closedを追加設計 |
| Meet | Calendar insertでconference createRequest、Meet URL再読込 | 再利用可能な実装。実権限/API成功は未確認。Calendar Eventと同じcycle IDに紐付ける |

Google Chat通知は既存GASで実装箇所があるが、timeout/成功応答喪失時の重複余地あり。将来移管時はoutboxとprovider idempotency能力を確認する。

## 7. Production/HP安全境界・Phase B承認

この作業中、Production DB/Gmail/Sheets/GAS/Calendar/Meet/Chat/Workersへの接続・書込、migration適用、Cloud Run操作、PR操作は実施していない。`~/ClinicLeadLocalReadOnly`、HP Job/Controller/Lease、port 8510、HP関連processは操作していない。GAS reference sourceも変更していない。

Phase Bの着手承認事項:

1. GASとClinic Leadの新規Draft作成authorityを一本化する運用決定、または両方が従うshared lockの設計・GAS変更承認。
2. 対象Spreadsheet/GAS deployment/trigger ownerと、`ClinicLead_GAS連携`のwriter/owner/status contract確認。新規専用tabを使う場合はその承認。
3. 対象Gmail mailbox/API read scopes、および検証mailboxでのheader/query/Trash/Scheduled read-only検証承認。
4. 対象Sheet data rowsのread-only照合、PII表示/保管/削除期間の承認。
5. Supabase schema、RLS/ACL、専用role、migration履歴確認と、後日のmigration/applyを別々に承認。
6. Sheets projector apply、GAS登録、Gmail Draft作成、Calendar/Meet/Chat操作は個別の非本番canary承認。Production反映はさらに別承認。

## 8. Phase Bローカル実装

### 共通Draft API契約

`src/outreach/draft_creation_api.py`にframework-neutralな`SharedDraftCreationAPI.create_or_recover()`を追加した。HTTP/auth adapterはまだなく、GASから到達できるAPIとしてはデプロイされていない。GAS/Clinic Lead双方が同じoperation/campaign/clinic identityとmetadata header契約を使うためのcoreである。

`src/repository/shared_draft_lease_repository.py`に注入済みPostgres connectionからlease SQL関数を呼ぶadapterも追加した。接続関数や環境URLの探索は持たず、テストはfake connectionを使用する。expiry後のUNKNOWN化はSQL関数が担当し、DB記録の曖昧さを理由にGmail作成を自動再試行しない。

概念上のrequest:

```json
{
  "operationKey": "verified-operation-key",
  "clinicUuid": "123e4567-e89b-42d3-a456-426614174000",
  "clinicId": 101,
  "campaignId": "campaign-id",
  "gasCaseId": "20261020-120000-123",
  "recipient": "clinic@example.jp",
  "subject": "subject",
  "body": "draft body"
}
```

Identity verified flagは内部coreにのみあり、公開payloadの自己申告を信頼してはならない。HTTP adapterがDB上のconfirmed UUID link、認証済みservice principal、campaign authorizationを確認してからcore requestを作る必要がある。Draft metadataは`DraftApiRequest.gmail_headers`が生成し、GAS案件ID、operation ID、Clinic ID/UUID、campaign、共有Draft keyを含む。共有Draft keyは`clinic-outreach-v1|clinic_id|campaign_id`のSHA-256で、既存Clinic Lead workflowの`_draft_key()`と同じ。

処理順は、atomic lease claim → Gmail Draft/Scheduled/Sent/Reply/Trashのread-only照合 → exact Draft key回収またはHOLD → Draft作成 → fenced DB completion。Gmail作成timeout時は一度だけmetadataを再照会し、exact Draft keyが見つかれば回収、そうでなければHOLDする。DB記録失敗後の再実行も先にGmail照合する。UUID mismatch、非exactでも同一医院/宛先の履歴、複数一致、状態不明は新規作成を止める。

`gmail_drafts.py`にGAS案件ID用metadata headerと`build_shared_draft_key()`を追加し、Gmail metadata readに含めた。`gmail_workflow.py`は共有keyを利用し、stable operation keyをoutreach headerに記録する。既存フォームやGAS本体は変更していない。

### Lease/unique/RLS migration案

未適用migration `supabase/migrations/20261020100000_shared_gas_clinic_draft_api.sql`を追加した。Shared operation key、clinic+campaign unique、GAS case ID unique、Gmail Draft/Message ID unique、lease owner/expiry/fencing token、operation event、GAS case identity link、Calendar cycle、sync previewを定義する。lease取得はtransaction内`FOR UPDATE`。作成処理中のleaseが期限切れになった場合はUNKNOWNへ固定してfencing tokenを増加し、自動再作成を止める。exact DraftをGmailで回収できれば台帳を照合し、見つからない場合は人の解決までHOLD。古いworkerはtoken/owner/expiry不一致で完了できない。

既存の`clinic_draft_guard`もAPI lease claim内で使う。同一clinicの別campaignは`CROSS_CAMPAIGN_HOLD`となり、既存のclinic-wide policyを緩めない。RLS FORCEと`clinic_outreach_runtime`だけのread/executeを想定し、直接table writeはrevoke、状態書込はsecurity-definer function経由。GAS/Clinic Lead両方の接続は将来APIサービスに集約する。migration構文の実DB検証、role/RLS検証、適用は未実施。

### Sheets同期preview

`src/outreach/gas_sheet_sync_preview.py::build_sheet_sync_preview()`はsource key単位で`INSERT/UPDATE/NOOP/HOLD`を返すだけで、Sheetsに接続しない。確認済みUUIDと検証済みGmail状態を要求する。投影UUIDが異なる、Gmail状態がunknown、identity conflict、preview後の行hash変化はHOLD。医院単位にまとめずsource row/案件単位の別履歴を維持する。

### Reply/Calendar/Meet contracts

`src/outreach/followup_contracts.py`に返信Draft用`ReplyDraftProvider`と、appointment cycle keyを持つ`CalendarMeetProvider`を追加した。送信/予約を行う実装はない。Calendar/MeetのDB案はmigrationの`shared_calendar_cycles`。返信文自動生成と日程調整URLは引き続き未実装。

### Phase B tests

人工fixture/memory store/Gmail metadata fakeで二重クリック、GAS/Clinic Lead concurrent request、Gmail作成後DB障害、Gmail timeout、lease期限切れ/fencing、期限切れ結果不明時の再作成禁止、Draft→Scheduled、operation再実行、別campaign、UUID不一致、Trash/unknown、Sheets行hash競合を確認した。関連16ファイルの最終実行結果は **163 passed**。`--basetemp=/private/tmp/...`を指定してSQLite並行テストも通過。Gmail/DB/Sheets/GAS実接続はなし。

既存の人工履歴fixtureでSQLite初期化と同一process内予約が同時実行されるときの競合も確認し、ローカル専用`OutreachLedger`の初期化/予約にpath単位lockを追加した。Postgres/Productionの共通leaseとは別のtest-fixture安定化である。
