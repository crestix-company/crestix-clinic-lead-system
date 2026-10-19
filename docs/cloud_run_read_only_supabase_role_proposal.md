# Cloud Run READ ONLY Supabase Reader — PR #122対応の最終適用案

更新日: 2026-10-20 JST。**本番適用は未承認・未実行。PR #118はDraftを維持する。**
この文書は以前の直接Clinic/Comdesk GRANT案と追記を置き換える。過去の診断履歴はGit履歴に保存されている。
旧版のSQLブロックを実行しないこと。

## 対象コミットと成果物

| 対象 | 確認したSHA |
|---|---|
| main | `c5829aa38a6d2fecc3023ffaa38181ab7e41f02f` |
| PR #121 | `0f88ed24fd7eba54c82de11dd7ca99d8e4b46437` |
| PR #122 | `67f159a08dfdcc8546a437138afc3e040abbbd15` |
| PR #118の更新前 | `e433266b35a3a8591f5c254449d45e70cbe73638` |

- [適用前カタログ検査](cloudrun_reader_release/catalog_check.sql): SELECTのみ。秘密値・患者/医院データを取得しない。
- [統合適用SQL](cloudrun_reader_release/apply.sql): DBAレビュー・個別承認後に一度だけ実行する案。現時点で実行しない。
- [適用後カタログ検証](cloudrun_reader_release/verify.sql): SELECTのみ。すべての`*_ok`がtrueであること。
- [Reader実接続確認](cloudrun_reader_release/reader_smoke.sql): 専用Reader接続でのみ実行する読み取りトランザクション。
- [限定ロールバック](cloudrun_reader_release/rollback.sql): 新規オブジェクト・新規Reader権限だけを撤去する案。

`apply.sql`は#122 Migrationの本体をそのまま埋め込み、事前条件、他5テーブルの限定GRANT/RLS、
新規オブジェクトのデフォルトACL除去を同じトランザクションに追加している。
`projection_67f159a.sql`と`projection_rollback_67f159a.sql`は#122の原文スナップショットであり、単独で適用しない。
生成は`python scripts/build_cloudrun_reader_release.py`。生成物は通常のSQLでDashboardにも貼り付け可能。
#122のMigrationを先に実行してからこの統合SQLを実行すると衝突するため、どちらかを重複実行しない。
今回の配布物は`docs/`配下で、Migration自動適用対象ではない。

## 今回実施した本番候補カタログのSELECT検査

既存Clinic Master監査とDashboard接続情報で記録されているproject ref
`hwdvyhunozporfuefteb`（表示名`clinic-lead-shadow`）に対し、Supabase MCPでSELECTのみ実行した。
別の`CRM`という名前のプロジェクトに切り替えていない。Cloud Runサービスがこのprojectを利用するという
サービス所有者の最終確認は引き続き必要。

確認結果:

- Readerは存在し、LOGIN=true、INHERIT=true、SUPERUSER/BYPASSRLS/CREATEDB/CREATEROLE/REPLICATION=false。
- Reader自身をmemberとするrole membershipはない。逆方向に`postgres`がReaderのmemberであり、
  ADMIN=true、INHERIT=false、SET=false。これはReaderがpostgresになれる経路ではない。変更しない。
- 元テーブル7件はRLS=true、FORCE RLS=false、owner=postgres。対象の既存ポリシーは
  `clinic_runtime`、`clinic_readonly`、`clinic_email_target_reader`向け。PUBLIC/Reader向けポリシーはない。
- Readerには`public.clinics`のtable-wide SELECTが残る。したがって`effective_json`の列権限も残る。
  Reader向けRLSポリシーがないことは、この過大なGRANTを許容する理由にはならない。
- 他6元テーブルのReader SELECTはfalse。7元テーブルのINSERT/UPDATE/DELETE/TRUNCATEはfalse。
- Clinic/Comdeskの専用VIEW、projection関数、helper roleは未作成。
- `effective_json`はtext、`signals_json`、Comdeskの`row_json`、templateの`headers_json`/`mapping_json`はjsonb。
- **追加発見:** postgresがpublicに作るrelation/functionには、デフォルトACLにより
  anon/authenticated/service_roleへの権限が付く。#122のPUBLIC EXECUTE撤去だけでは除去できない。

この追加発見後、本番への追加検査・適用は停止した。統合SQLでは、今回新規作成する2VIEWと1関数のACLだけを
owner以外いったん撤去し、ReaderのSELECTとReader/helperの関数EXECUTEだけを付与する。
既存CRMオブジェクト、データ、default privileges、既存role membership、schema全体のPUBLIC権限は変更しない。
カタログ検査ではパスワード・ハッシュ・接続文字列・医院レコードを取得していない。

## Readerの7参照先と列契約

| 参照先 | Reader SELECT列 |
|---|---|
| `public.clinic_cloudrun_readonly` | 下記の投影列のみ |
| `provenance.clinic_cloudrun_comdesk_rows` | `id,clinic_id,template_id,uuid,projection_valid,row_json`（投影済み配列） |
| `hp_research.clinic_hp_research` | `clinic_id,machine_hp_rank,fetch_status,hp_url,final_url,treatment_categories` |
| `treatment.clinic_treatment_research` | `clinic_id,research_status,treatment_category_name` |
| `treatment.clinic_research_status` | `clinic_id,research_status` |
| `provenance.templates` | `id,headers_json,mapping_json`。Comdesk VIEWに参照行があるtemplateのみ |
| `public.clinic_email_enrichment` | `clinic_id,email,status,verified_on_official`。確認済み公式メールのみ |

Readerに`public.clinics`や`provenance.comdesk_original_rows`の直接SELECTを付与しない。
Migrationは既存のtable/column SELECTを撤去し、PUBLIC・継承経路を含む実効権限が残れば中止する。

Clinic VIEWの元列（型は元テーブルの型をそのまま継承し、`catalog_check.sql`で再確認）:

```text
id,uuid,merged_into,tel_match_key,first_seen_at,last_seen_at,source_as_of_date,
is_new,departments_json,hp_status,hp_url,hp_rank,maps_website_url,
clinic_name,phone,address,prefecture,medical_type,active,designation_date,
recent_until,registration_reason,owner_equal,signal_count,hot_status,treatments_json,
signals_json,merge_hold,maps_presence_status,maps_profile_url,exclude_reason
```

追加列は`effective_projection jsonb`、`projection_marker text`、`projection_valid boolean`、
`is_hospital_or_center boolean`。投影JSON内のキーは`hp_production_companies`（文字列配列）、
`epark_url`/`epark_contract`（文字列またはnull）の3つだけ。
raw facility_type、年齢推定、owner_name、manager_name、診療時間、根拠JSONは含まない。
施設除外booleanは通常モードと同じliteral LIKE条件で算出し、医院名・exclude_reason条件と併用する。

Comdesk VIEWの`row_json`は元行そのものではない。元位置を維持して次の18ヘッダーだけ値を残し、
その他は空文字にする。`projection_valid`はboolean、`row_json`はjsonb配列、ID/UUID列は元の型を継承する。

```text
UUID,種別,名前,カナ,郵便番号,都道府県,住所１,住所２,住所カナ,
Tel1,Tel2,Tel3,Tel4,FAX,URL,旧社名,リードソース,開業日
```

院長名・診療日/時間・休診日・備考・履歴・記事は空欄。配列の型違い・列数不一致は出力中止。
標準30列のCSV/XLSX、元の名前/電話優先順位、HPランク、公式確認済みメールの条件は維持する。
メールのRLS条件は`status='VERIFIED_EMAIL' AND verified_on_official IS TRUE AND NULLIF(BTRIM(email),'') IS NOT NULL`。
公式確認済み連絡先メールをIAP許可済み社内利用者に出力する仕様はユーザー承認済みであり、再承認を要求しない。

## 承認が未確認の列

コード上の列契約は確定したが、この会話には残るすべての列のデータオーナー承認記録がない。
特にComdeskの上記18ヘッダー（名前/カナ/旧社名/リードソース/開業日を含む）、
`epark_url`/`epark_contract`、`owner_equal`、マーケティングsignal/HP情報の社内公開範囲を確認する。
承認待ちのowner_name/manager_name/年齢推定/診療時間を新しく追加していない。
未承認列を残したまま本番適用しない。除外が必要な場合は列契約と出力テストを更新する。

## VIEW owner・RLS・ACL

ownerは新規NOLOGIN/NOINHERIT/NOBYPASSRLS/NOSUPERUSER helperで、元テーブルownerではなく、他roleのmemberではない。
helperにのみ投影に必要な元列権限を付与し、ReaderにはVIEWを付与する。
`security_invoker=true`にするとReaderの元列権限が再び必要になるため、この確定済み設計では使用しない。
PostgreSQLはこのVIEWの元テーブルアクセスにownerのRLSを適用する。
[PostgreSQL CREATE VIEW仕様](https://www.postgresql.org/docs/current/sql-createview.html)

helper専用のpermissive SELECT policyは承認対象の全国行を許可する。Reader/helperに既存role membershipがなく、
PUBLIC policyがないことを事前確認する。restrictive policyがあれば引き続き制限される。
既存CRM向けポリシーを変更しない。helperのschema CREATEは作成中だけ付与し、最終的に実効falseを確認する。
関数はcreate-only/SECURITY INVOKERで同名衝突を拒否し、PUBLICおよびdefault ACL由来の不要EXECUTEを撤去する。

非superuser/CREATEROLEのpostgresで#122をそのまま実行するとVIEW ownership transferが失敗することも隔離DBで確認した。
統合案は`SET LOCAL createrole_self_grant = 'SET, INHERIT'`で新規helperの作成者に一時権限を与え、
最後にpostgres自身が付与したmembershipを撤去する。自動作成されるDBAのADMIN-only逆方向edgeだけを保持する
（postgres→helperのINHERIT=false/SET=false）。Readerのhelper membershipは一切認めない。
rollbackでは新規helperの管理者として一時INHERIT/SETを取得し、helper自体を削除する。
既存CRM roleのmembershipを変更しない。このDBA管理edgeは#122単体の「helperにmembershipなし」という説明への
明示的な設計補足であり、本番適用前のDBAレビュー対象。実効状態はapply内とverify.sqlの両方で検査する。

## 適用SQLの完成状態と順序

SQLファイル一式は隔離DBで実行可能・検証済み。ただし本番適用GOではない。
事前条件は今回のcatalog baseline専用であり、差異があれば既存設定を書き換えず中止する。
対象が既に適用済みの場合も再実行を拒否する。

1. レビュー承認後のコード統合順は#118（文書/手動SQL）、#121、#122。#122は#121をbaseとするため、
   #121統合後にbaseをmainへ確認/調整し、その最終headでCIを通す。Draft解除・mergeは今回行わない。
2. DBAが対象projectと最新catalog、投影列承認、既存role/policy/default ACL、全国件数の基準を確認する。
3. 別途本番適用承認後、`apply.sql`だけを一度実行する。#122 Migrationの単独実行や自動Migrationは併用しない。
   一つのBEGIN/COMMIT内でhelper/VIEW、限定GRANT、RLS、default ACL除去、実効権限検証を完了する。
4. `catalog_check.sql`と`verify.sql`で適用後のACL/owner/RLS/列境界を確認する。
   Reader実接続で`reader_smoke.sql`、全国件数照合、検索・出力を確認する。本番DML拒否試験は行わない。
5. 失敗時は公開を進めない。トランザクション中ならROLLBACK。commit後の限定撤去には別途承認した`rollback.sql`を使う。
   後から追加されたhelper依存ポリシー等があれば停止し、CASCADEで消さない。
   raw Clinic/Comdesk SELECTを復活させず、Reader画面を停止した状態で原因を調査する。
6. DBとReader接続の確認後、A担当が承認済み手順でSecret参照、build、no-traffic revision、IAP許可/未許可アクセス、
   かんたん操作・営業対象/出力を検証する。Cloud Run deploy/traffic変更は今回の範囲外。

## 実施した検証と未検証

- PostgreSQL 17.11の使い捨てloopback clusterに、#122のNULL/不正JSON/escaped-NUL/型違いの合成fixtureを再利用。
- 実際のSupabaseと同じanon/authenticated/service_roleのdefault privilegesを再現し、新規VIEW/関数アクセスが除去されることを確認。
- 非superuserかつCREATEROLEのpostgresを使い、統合apply/verify/Reader smokeが成功。全国検索・都道府県・診療科・HPランク・治療・制作会社・ページング・CSV/XLSX、
  公式メール、Clinic ID重複、生JSON/元Comdesk行SELECT拒否、Reader DML拒否、SQL捕捉を#122アダプターで検証。
- helperへのrestrictive RLSが効き、既存CRMのpolicyとUPDATE権限が維持されることを確認。
- 再適用は停止。他者追加の依存policyがあるrollbackは全体をrollback。
  そのfixture専用依存を除いた後の限定rollbackは成功し、元テーブルを保持・raw Reader権限を復活させない。
- #122アプリ自体は変更していない。既報のPython 3.12 full pytestは2,204 passed / 55 skipped。
  今回は変更した統合SQLに対する隔離試験を追加した。100,000件上限の既存fail-closed仕様も変更していない。
- CIで既存メール生成テスト22件が日付依存で失敗したため、3テストファイルの`GenerationSettings.as_of`を
  固定fixtureの2026-10-19に明示した。予定日が実行日の翌日以降かどうかという本体チェックやGmail gateは変更していない。
  関連40テストは成功。DB設計とは別のテスト安定化commitとしてレビュー可能。
- 本番Reader認証、全国count parity、本番規模の性能、Cloud Run/IAPは未検証。

再現用（別名のbootstrap管理者でinitdbし、非superuser/LOGIN/CREATEROLE/CREATEDBのpostgresを作成してDB ownerにした、新しい使い捨てlocalhost DBでのみ使用）:

```text
CLINIC_RELEASE_TEST_DSN=<loopback-only disposable DSN> python scripts/verify_cloudrun_reader_release.py --implementation <PR122-checkout>
```

## Reader認証とユーザーのDashboard操作

この環境では`CLINIC_CLOUDRUN_READER_DB_URL`、`SUPABASE_READER_DB_URL`、`SUPABASE_RUNTIME_DB_URL`の存在確認のみ実施し、
すべて未設定だった。roleのLOGIN可否はパスワードの設定・有効性を証明しない。パスワード/ハッシュは読まず、
Readerの実認証は未実施。手元にある`clinic_runtime`資格情報を流用しない。

今ユーザーがDashboardで必要な操作は、正しいprojectを選び、ConnectでSession Poolerのproject/host/portを確認すること。
資格情報管理者が既存Readerパスワードの保管場所と利用可否を確認し、チャット/GitHubに秘密値を貼らない。
未設定ならReaderパスワードの新規設定を個別承認してから行う（今回のSQLにはパスワード操作を含めない）。
後日DB適用が明示承認された場合だけ、SQL Editorで再preflight→統合apply→verifyの順に実施する。

**本番DB適用判定: NO-GO。** 残る条件は対象projectの最終承認、残る投影列の承認、
再preflight、統合SQLのDBA承認、専用Reader資格情報と実接続確認。準備作業としてのSQL一式と隔離検証は完了。
