# Treatment Research sidecar契約（Filter/UI/CSV開発セッション向け）

このファイルは Filter/UI/Comdesk CSV担当セッション（`development/v4-base`）が、
Treatment Research担当セッション（`research/v4-base`）の出力を読むために定義した契約。
**Treatment Researchのevidence engine・taxonomy・判定ロジックはこのセッションでは変更しない。**
このドキュメントは「Filter側がどこを読みにいくか」だけを定義する。

## 場所（共通Runtime DB・単一のSource of Truth）

Research Worker（`research/v4-base`、別worktree）とFilter/UI/CSV（`development/v4-base`、
このworktree）は別々のworktreeで並行動作する。worktree内の相対パスをsidecarにすると
互いのプロセスが別々のSQLiteファイルを見てしまい、Research側の増分結果がFilter側に
反映されない。そのため**worktreeに属さない共通の絶対パス**を単一のSource of Truthとする。

```
~/CrestixData/clinic-lead/treatment_research_final.sqlite3
```

環境変数 `TREATMENT_RESEARCH_DB_PATH` で上書き可能（デフォルトは上記パス）。

役割分担:
- **Research Worker: WRITE**。このファイルへ書き込むのはResearch Worker側のみ。
- **Filter/UI/CSV: READ ONLY**。`src/master/store.py` の `ClinicStore.connect()` が
  SQLite URI `mode=ro` で `ATTACH DATABASE 'file:...?mode=ro' AS researchdb` する
  （`src/master/research_sidecar.py`）。書き込みはSQLite自体が物理的に拒否する。

`./data/clinics.sqlite3`（Production Clinic Master）には一切書き込まない。**Git管理しない**
（`*.sqlite3`はリポジトリ外のパスであり、かつ既存`.gitignore`のパターンにも該当する）。

このファイルが存在しない間、Filter側は「HP治療カテゴリ」「Research Status」の
2 filterを安全に0件/非表示扱いにする（`src/master/research_sidecar.py` の
`research_sidecar_available()` / `clinic_research_status_available()`）。
**ダミーデータを本番用として生成することはしない。**

## テーブル（2026-10-01改訂: 2テーブルに役割分離）

Research側がsidecarの契約を分離した（`research/v4-base` commit `90bb9fe`）。
「医院単位の最新attempt状態」と「Treatment単位の最新成功結果」を1つのテーブルに
混在させていた旧契約を廃止し、以下の2テーブルに分離する。

### `clinic_research_status`（新設・医院単位SSOT。Research Status filter用）

```sql
CREATE TABLE clinic_research_status(
  clinic_id INTEGER PRIMARY KEY,
  research_status TEXT NOT NULL CHECK(research_status IN ('DONE','FETCH_FAILED')),
  candidate_count INTEGER NOT NULL DEFAULT 0,
  source_url TEXT NOT NULL DEFAULT '',
  final_url TEXT NOT NULL DEFAULT '',
  identity_verified INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT NOT NULL DEFAULT '',
  taxonomy_version TEXT NOT NULL,
  evidence_engine_version TEXT NOT NULL,
  manifest_id TEXT NOT NULL,
  git_commit_sha TEXT NOT NULL,
  researched_at TEXT NOT NULL
);
```

- `research_status` は `DONE` / `FETCH_FAILED` の2値のみ。**`NOT_RESEARCHED` はテーブルに行を
  作らないことで表現する**（その clinic_id が未調査であることを意味する）。
- 1医院1行（`clinic_id` が PRIMARY KEY）。`DONE` かつ `candidate_count=0` の医院も1行持ち、
  `NOT_RESEARCHED` には該当しない。
- 「Research Status」filterはこのテーブルだけを見る。`clinic_treatment_research_final`の
  行の有無・値からResearch Statusを推測してはならない。

### `clinic_treatment_research_final`（Treatment単位・既存。HP治療カテゴリfilter用）

```sql
CREATE TABLE clinic_treatment_research_final(
  clinic_id INTEGER NOT NULL,
  treatment_category_id TEXT NOT NULL,
  treatment_category_name TEXT NOT NULL,
  research_status TEXT NOT NULL CHECK(research_status IN ('CONFIRMED','REVIEW','NOT_CONFIRMED')),
  matched_alias TEXT NOT NULL DEFAULT '',
  source_url TEXT NOT NULL DEFAULT '',
  page_title TEXT NOT NULL DEFAULT '',
  provider_context TEXT NOT NULL DEFAULT '',
  exclusion_context TEXT NOT NULL DEFAULT '',
  evidence_engine_version TEXT NOT NULL DEFAULT '',
  taxonomy_version TEXT NOT NULL DEFAULT '',
  researched_at TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(clinic_id, treatment_category_name)
);
```

- `clinic_id` は既存 `clinics.id`（legacy inner ID）をそのまま使う。医院名JOIN・電話番号JOINは禁止。
- `research_status` は `CONFIRMED` / `REVIEW` / `NOT_CONFIRMED` の3値のみ。**`FETCH_FAILED` と
  `NOT_RESEARCHED` はこのテーブルにはもう入らない**（CHECK制約で禁止。旧契約の`FETCH_FAILED`行は
  移行済みで現在0件）。一時障害（FETCH_FAILED）で過去の成功結果を消す設計ではないため、
  `clinic_research_status`が`FETCH_FAILED`の医院でも、過去にCONFIRMEDした行が残っていてよい。
- 1医院・1治療カテゴリにつき1行。複数治療カテゴリを持つ医院は複数行になってよい
  （Filter側は `EXISTS` で判定するため、一覧・CSVの1clinic=1行原則は崩れない）。
- Research対象はCrestix9カテゴリに限定しない（Research側方針変更に合わせ、Filter側もこの
  テーブルをNavi正式診療科・Crestix営業カテゴリと独立したJOIN軸として扱う）。
- 増分追従: Research側がこのテーブルに行を追加していくだけでよい。Filter側のコード変更は不要
  （`src/master/filters.py` は常にこのテーブルを都度クエリする）。

## Filter側の利用ルール

- 「HP治療カテゴリ」filterで治療カテゴリを指定した場合、`clinic_treatment_research_final.research_status
  ='CONFIRMED'` の行のみ営業対象に含める。`REVIEW` / `NOT_CONFIRMED` / 行なしは除外する
  （契約変更なし）。
- 「Research Status」filterは上記ルールとは完全に独立した医院単位の調査状況フィルタで、
  `DONE` / `FETCH_FAILED` / `NOT_RESEARCHED` を選択できる（`clinic_research_status`のみを見る）。
  `clinic_research_status`が`DONE`であることは、`clinic_treatment_research_final`にCONFIRMED行が
  存在することを意味しない（`candidate_count=0`のDONE医院はHP治療カテゴリfilterには自然に
  ヒットしない）。逆もまた真で、`clinic_research_status`の値を理由にCONFIRMED行を無視・除外しない。

## 変更履歴

- 2026-09-30: 初版。Filter/UI/CSV担当セッションが定義。
- 2026-10-01: Research側のsidecar契約分離（`research/v4-base` commit `90bb9fe`）に合わせ、
  Research Status filterの参照先を`clinic_treatment_research_final`から新設`clinic_research_status`
  （医院単位SSOT）へ移行。HP治療カテゴリfilterの仕様（CONFIRMED-only）は変更なし。
