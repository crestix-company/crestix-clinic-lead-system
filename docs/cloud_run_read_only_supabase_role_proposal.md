# Cloud Run READ ONLY Supabase Reader — PR #122対応の最終適用案

更新日: 2026-10-20 JST。**本番適用は未承認・未実行。PR #118はDraftを維持する。**
この文書は以前の直接Clinic/Comdesk GRANT案と追記を置き換える。過去の診断履歴はGit履歴に保存されている。
旧版のSQLブロックを実行しないこと。

## 対象コミットと成果物

| 対象 | 確認したSHA |
|---|---|
| main | `c5829aa38a6d2fecc3023ffaa38181ab7e41f02f` |
| PR #121 | `0f88ed24fd7eba54c82de11dd7ca99d8e4b46437` |
| PR #122 (latest checked) | `74d89afee68fd6c86bc44b042048f6f618b43c39` |
| PR #118の更新前 | `e433266b35a3a8591f5c254449d45e70cbe73638` |

- [適用前カタログ検査](cloudrun_reader_release/catalog_check.sql): SELECTのみ。秘密値・患者/医院データを取得しない。
- [統合適用SQL](cloudrun_reader_release/apply.sql): DBAレビュー・個別承認後に一度だけ実行する案。現時点で実行しない。
- [適用後カタログ検証](cloudrun_reader_release/verify.sql): SELECTのみ。すべての`*_ok`がtrueであること。
- [Reader実接続確認](cloudrun_reader_release/reader_smoke.sql): 専用Reader接続でのみ実行する読み取りトランザクション。
- [限定ロールバック](cloudrun_reader_release/rollback.sql): 新規オブジェクト・新規Reader権限だけを撤去する案。

`apply.sql`は#122 Migrationの本体をそのまま埋め込み、事前条件、他5テーブルの限定GRANT/RLS、
新規オブジェクトのデフォルトACL除去を同じトランザクションに追加している。
`projection_74d89afe.sql`は#122 head `74d89afe`を基に、サービス所有者承認済みのEPARK課金情報除外を重ねた提案Migrationである。C担当が同内容を#122へ反映後に一対一再照合する。`projection_rollback_74d89afe.sql`は#122 headのrollback案。どちらも単独で適用しない。
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
id,uuid,merged_into,first_seen_at,last_seen_at,source_as_of_date,
is_new,departments_json,hp_status,hp_url,hp_rank,maps_website_url,
clinic_name,phone,address,prefecture,medical_type,active,designation_date,
recent_until,registration_reason,owner_equal,signal_count,hot_status,treatments_json,
signals_json,merge_hold,maps_presence_status,maps_profile_url,exclude_reason
```

追加列は`effective_projection jsonb`、`projection_marker text`、`projection_valid boolean`、
`is_hospital_or_center boolean`。投影JSON内のキーは`hp_production_companies`（文字列配列）と
`epark_url`（文字列またはnull）の2つだけ。`epark_contract`は含めない。
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

## 公開列の承認状態

サービス所有者の2026-10-20方針は、IAP許可ユーザー向けに既存ローカル版相当の営業検索・抽出・標準出力を維持する目的承認である。
これはClinic VIEWの全列や契約情報を無条件に公開する承認ではない。列ごとに必要な機能、画面/出力先、DB Readerから直接読める範囲を確認する。
Comdesk基本30列と公式確認済みメールの出力機能は維持する。メールは条件付きRLSを維持する。
院長/管理者の氏名、年齢推定、元のeffective_json、Comdesk raw row、HPページ本文/根拠、監査・履歴・メール配信履歴はReaderへ公開しない。
契約情報は後段の列マトリクスに示す個別判断を残す。

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

## 2026-10-20 最終差分確認

前回確認したhead（#118 `2d282afd8d5ffe1bbfa9042dd17956fa9b279cf5`、#121
`0f88ed24fd7eba54c82de11dd7ca99d8e4b46437`、#122
`85aabbec218b1e73124bc5143a3d5dee351fe3d3`）に基づく。本番候補projectのSELECT専用カタログ結果は
上記のベースラインと整合する。View/関数は未作成、ReaderのClinic table-wide SELECTは存在し、他6元テーブルの
Reader SELECTはなく、対象7テーブルのReader DML権限はない。RLSは7元テーブルすべて有効で、Reader向け
policyは未作成。#122のMigration投影本体と統合`apply.sql`内の固定投影本体は一致し、Migrationは`supabase/migrations`
に存在する一方、`docs/`の統合SQLは自動Migration対象外。適用時はどちらか一方だけを使う。

前回確認で見つかった`phone_norm`不整合は、#122 head `85aabbec218b1e73124bc5143a3d5dee351fe3d3`でVIEWへ列を追加し、統合SQLへ反映済み。医院名・電話・UUID検索を隔離DBで検証済み。

Reader認証の確認は環境変数の有無を秘密値なしで調べた結果、専用URLは未設定で、実認証も未実施。Supabaseのproject refと
ReaderのLOGIN属性だけではCloud Run接続先承認や有効なパスワードの証明にならない。サービス所有者による接続先承認、資格情報管理者による
Reader secretの安全な存在確認、専用ReaderによるSession Pooler実接続確認が残る。認証情報をログ/チャットへ出さず、
Cloud Run用Secret参照の登録はDB接続確認後にA担当が別承認で行う。

## 2026-10-20 phone_norm反映と統合SQL再検証

PR #122のhead `85aabbec218b1e73124bc5143a3d5dee351fe3d3` と最新CI（pytest SUCCESS）を確認した。
Migrationが`public.clinic_cloudrun_readonly.phone_norm text`を投影し、READ ONLY adapterと医院名・正規化電話・UUID検索テストが
同じ列契約を使うことを確認した。PR #118の統合SQLを最新Migration本体から再生成し、Migrationスナップショットと本体のSHA-256
一致、VIEW bodyのバイト一致、単一BEGIN/COMMIT、`verify.sql`の列型/SELECT権限検査、`reader_smoke.sql`の列解決確認、
`rollback.sql`の列権限撤去を確認した。phone_normの以前の検索不整合は解消。

新規クラスタを使った使い捨てPostgreSQL 17.11統合検証は成功。ReaderロールでのVIEW/RLS、電話番号・医院名・UUID検索、
全国fixture、7参照先境界、生JSON/raw Comdesk拒否、DML拒否、CSV/XLSX、公式メール条件、default ACL遮断、既存CRM policy保持、
再適用拒否、依存policyのあるrollback拒否と限定rollbackを確認した。本番データ/認証情報は使用していない。

この更新ではSQLパッケージと設計書だけを更新した。Reader資格情報は未設定/未確認のまま。公開列の最終分類は以下の
サービス所有者方針に合わせたマトリクスを正とする。契約関連の個別判断とDB接続準備が残るため、本番適用は **NO-GO**。

## 2026-10-20 サービス所有者確定方針に基づく公開列マトリクス

Cloud RunのReaderにはVIEW列全体のSELECTが付く。したがって「画面で非表示」はDB権限上の非公開を意味しない。
下表の「Reader可視」はDB Reader資格情報から読める列/値、「アプリ用途」はIAP画面・抽出・出力での使用先である。
営業機能の維持を前提としつつ、raw JSON・個人名・根拠記録は引き続き除外する。

| 列/参照先 | Reader可視の最小データ | アプリ用途・出力先 | 方針/個別承認 |
|---|---|---|---|
| `public.clinic_cloudrun_readonly`: `id,uuid,clinic_name,phone,address,prefecture,medical_type,departments_json` | 医院ID、UUID、医院の連絡/所在地/診療種別 | 全国検索、都道府県・診療科検索、一覧、Comdesk標準30列 | 機能維持。Comdesk出力は18項目allowlistから30列へ構成し、他の元値は空欄 |
| 同VIEW: `phone_norm` | 電話番号の数字正規化値（`phone`と重複） | 医院名/電話/UUIDキーワード検索のみ | 検索に必須。CSV/XLSXには出力しない |
| 同VIEW: `active` | 現存状態boolean | 営業対象の現存条件・件数 | 営業抽出に必要。行値としては一覧/CSVに出力しない |
| 同VIEW: `merged_into,merge_hold,exclude_reason,is_hospital_or_center` | 統合ID、保留状態、除外理由/病院・センター判定 | 重複防止、Merge hold、病院/センター除外 | 既存除外条件維持に必要。ユーザー向け一覧/Comdeskには出力しない。今後、同等条件をVIEWの行条件へ閉じ込められれば列権限を縮小可能 |
| 同VIEW: `first_seen_at,last_seen_at,source_as_of_date,is_new` | データ基準/更新時刻、追加判定 | 全国/旧来scope、更新差分フィルター、内部レコード処理 | 旧来scopeと詳細条件維持に必要。標準出力対象外。時刻値のUI表示は初回対象外 |
| 同VIEW: `designation_date,recent_until,registration_reason` | 公的指定年月日/有効期限/登録理由 | 開業10年以内・新規開業候補条件、一覧の指定年月日 | 営業抽出条件維持に必要。登録理由は条件評価用でCSVには出力しない |
| `public.clinics.tel_match_key` | Reader投影対象外。内部identity matching用の電話キー | 通常モードの照合。READ ONLY検索は`phone_norm`、医院名、UUIDを使う | PR #122最新headでReader adapter/View/owner GRANTから除外済み。READ ONLY検索・Comdesk出力に依存しないことをCI対象コードで確認。通常モードの`public.clinics`経路は変更しない |
| 同VIEW: `hp_status,hp_url,hp_rank` と `hp_research.clinic_hp_research`: `machine_hp_rank,fetch_status,hp_url,final_url,treatment_categories` | 公式HP URL、調査状態、旧/機械HP rank、転送先、確認済み治療カテゴリ | HP rank/status/URL表示、rank/site type検索、治療カテゴリ・Sales pair抽出、Comdesk URL補完 | 営業機能として維持。公開HP情報と状態のみ。ページ本文、crawl evidence、判定根拠JSONは含めない |
| 同VIEW: `departments_json,treatments_json`、`treatment.clinic_treatment_research`: `clinic_id,research_status,treatment_category_name`、`treatment.clinic_research_status`: `clinic_id,research_status` | 診療科/治療分類、Clinic別Research状態 | 診療科・治療カテゴリ検索、CONFIRMED側のSales pair条件 | 機能維持。Treatmentの根拠本文・調査履歴は対象外 |
| 同VIEW: `maps_presence_status,maps_profile_url,maps_website_url` | Maps掲載状態、MapsプロフィールURL、掲載HP URL | Mapsフィルター、詳細リンク、Comdesk URL補完、サイト種別分類 | サービス所有者が維持を指定。URL/状態のみ。Mapsのraw結果・一致根拠・個人投稿内容は対象外 |
| 同VIEW: `owner_equal` | 開設者と管理者の一致booleanのみ | 営業対象の詳細フィルター、一覧分類 | 名前/免許番号/元レコードはReader対象外。関係booleanの表示・抽出はサービス所有者の営業用途承認内。個人情報と扱う社内規程がある場合はprivacy owner確認が必要 |
| 同VIEW: `signal_count,hot_status,signals_json` | 数/温度区分/allowlist済みシグナル名配列のみ（根拠なし） | 営業対象判定、広告・集客施策フィルター、優先順、一覧/詳細 | 営業機能維持。根拠URL・引用文・担当者メモ・raw signal JSONは非公開。許可シグナル名を現行定義に固定 |
| 同VIEW: `effective_projection.hp_production_companies` | 公式HPで確認された制作会社名の配列のみ | HP制作会社フィルター | サービス所有者が維持を指定。証拠URL/本文は投影しない |
| 同VIEW: `effective_projection.epark_url` | 公開プロフィールURL | 一覧/詳細のリンク | 現行画面で表示、Comdesk標準出力には含めない。公開ページURLとして契約enumと分離して扱う |
| 同VIEW: `effective_projection.epark_contract` | **Readerには投影しない** | なし。EPARK URLは引き続き表示 | サービス所有者決定によりREAD ONLY初期公開から除外。契約enumを表示・検索しない |
| 同VIEW: `signals_json,signal_count,hot_status` | EPARK課金シグナルを除いたシグナル名配列、再計算件数と優先度 | 他の広告/集客シグナル検索、件数、並び順 | 課金由来シグナルは除外。他シグナルを維持し、表示件数と優先度は残存シグナルから再計算。元テーブルの集計値をそのままReaderへ渡さない |
| `provenance.clinic_cloudrun_comdesk_rows` | Clinic ID/template ID/UUID、投影有効フラグ、18 allowlist値を元位置に保持した配列 | 既存Comdesk値の優先順位維持、標準30列CSV/XLSX | Readerへraw rowを付与しない。院長名、診療日/時間、備考、履歴、記事などallowlist外要素は空欄 |
| `provenance.templates`: `id,headers_json,mapping_json` | 対象投影行が参照する列見出し/マッピングmetadata | 18 allowlist値を元CSV位置から標準30列へ写像 | 値行は含まない。任意ヘッダー名・全template metadataの露出を避けるためRLSは投影VIEWから参照されるtemplateに限定。出力仕様維持の最小metadataとして許可 |
| `public.clinic_email_enrichment`: `clinic_id,email,status,verified_on_official` | 条件を満たす公式確認済みメールのみ | 営業対象CSV/XLSXのメール列 | 既承認。RLS条件は`VERIFIED_EMAIL`、`verified_on_official IS TRUE`、非NULL/空欄以外を維持 |

### Readerに引き続き許可しない情報

- `public.clinics.effective_json`、Comdeskのraw `row_json`、HP調査raw JSON/ページ本文、treatment evidence、メール調査候補/履歴、監査/変更履歴、配信履歴/KPI、HP Job状態。
- owner/managerの氏名・年齢推定・年齢根拠、診療時間等のComdesk allowlist外列。
- EPARK契約enum/課金由来シグナル/その評価加点、契約書・証憑・確認メモ、制作会社の証拠文/URL、マーケティングsignalの根拠URL/引用文。

### 今回残る承認・検証

1. C担当がREAD ONLY UI/検索/評価からEPARK契約状態と課金シグナルを除き、他の集客シグナル・営業機能が維持されたことをテストする。
2. C担当が#122 Migrationを本設計のEPARK除外投影へ更新し、最新CIと3PR統合検証を通す。現在の#118投影SQLは#122 headを基にした提案overlayであり、Migrationとの一致はその後に確認する。
3. Reader専用資格情報の安全な確認とSession Pooler実認証、DBAによる本番適用承認。

その他の列は上表の営業目的・出力先に限定して維持する方針であり、「未承認列」という理由だけで一律に削除しない。

## 2026-10-20 PR #122 head 74d89afe 最終差分

PR #122は`85aabbec218b1e73124bc5143a3d5dee351fe3d3`から
`74d89afee68fd6c86bc44b042048f6f618b43c39`へ更新された。最新差分でREAD ONLY adapterの列契約、VIEW、投影ownerの列GRANTから
`tel_match_key`が除外され、電話検索に必要な`phone_norm`は残った。PR #122の検索テストは医院名・正規化電話・UUIDを確認し、Comdesk出力へ`phone_norm`を追加しない。
この実装差分をC担当の判定として採用し、DBビューのReader投影に`tel_match_key`は不要と確定する。通常モードの列契約は変わらない。

最新Migration `20261019195000_cloudrun_readonly_clinic_view.sql` とロールバックを
`projection_74d89afe.sql` / `projection_rollback_74d89afe.sql`として固定し、
`build_cloudrun_reader_release.py`から`apply.sql`/`rollback.sql`を再生成した。
統合SQLではReaderが`phone_norm`をSELECTでき、`tel_match_key`の列権限とVIEW列が存在しないことを
`verify.sql`でも確認する。独立した#122 CIは確認時点でまだrunが報告されていない。#118と#121のpytest checksは成功。
従って最新headの統合コードGO判定は#122 CIとC担当の一時統合pytest待ちである。

今回の再確認では、本番候補DBにSELECTのみ実施した。投影VIEW/関数/helper roleは未作成、Readerには`public.clinics`の
table-wide SELECTが残存、他6元テーブルにReader SELECTはなく、対象テーブルReader DMLもない。7元テーブルのRLSは有効で
FORCE RLSではなく、既存policyはCRM等のロールに限定される。ReaderはINHERIT=true、属性上の昇格フラグは無効。
Reader自身は他ロールのmemberではなく、`postgres`が逆向きにReaderのmember（ADMIN=true、INHERIT/SET=false）。
対象ソースにPUBLIC明示ACLは見つからなかった。既存CRM権限・policyは変更しない。

PR #122最新CIの成功、C担当の3PR統合競合/テスト確認、本番DBA適用承認、Reader Session Pooler実認証は未解決。
サービス所有者はEPARK契約enumと課金由来signalの初期公開除外を決定済み。C担当のMigration反映を確認後、#122 Migrationと統合`apply.sql`を二重適用しないこと。

## 2026-10-20 EPARK課金情報除外の隔離検証

PR #118の設計更新commitは`4edd3e8`（Draft維持）。
サービス所有者決定に基づき、#118提案Migrationから`effective_projection.epark_contract`を除き、
`signals_json`から正確な値`EPARK課金済み確認`をReader向けに除外する。Reader向け`signal_count`と`hot_status`は、
残ったsignal配列から再計算する。元の`signal_count`/`hot_status`列はprojection ownerへGRANTしない。
EPARK掲載URL、HP制作会社名、他のsignal名は維持する。#122 latest `74d89afe`時点のC担当Migrationにはこのoverlayがまだ反映されていないため、
マージ前にC担当が同じロジックをMigration/アプリ実装へ反映し、最新版headと再照合する。

隔離PostgreSQL 17.11でこの統合SQLを適用し、verify.sqlの全`*_ok`がtrue、Reader smokeで全国fixture 5件、
禁止EPARK課金情報露出0件、read-only transaction有効を確認した。5 fixtureは課金由来signalのみ/課金signal+1件/課金signal+2件/
課金なし2件/課金signal+3件のケースを含み、Reader出力は残存数0/1/2/2/3、優先度「通常/集客投資シグナルあり/アツい/アツい/かなりアツい」と一致。
EPARK URLとHP制作会社投影も残り、Google広告、SNS、独自LP、制作実績、昼枠のsignalは維持された。Readerの元effective_json/signals_json参照とUPDATEは拒否され、
限定rollback後にClinic行・既存CRM policyが保持され、Readerへの元表SELECTが復活しないことを確認した。

これはDB境界の隔離テストであり、READ ONLY UIの契約状態/検索条件除去、CSV/XLSX回帰、#118/#121/#122の統合pytest/CIを代替しない。
