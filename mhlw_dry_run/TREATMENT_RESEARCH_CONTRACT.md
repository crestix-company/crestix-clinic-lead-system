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
`research_sidecar_available()`）。**ダミーデータを本番用として生成することはしない。**

## テーブル

```sql
CREATE TABLE clinic_treatment_research_final(
  clinic_id INTEGER NOT NULL,
  treatment_category_id TEXT NOT NULL,
  treatment_category_name TEXT NOT NULL,
  research_status TEXT NOT NULL,          -- CONFIRMED / REVIEW / NOT_CONFIRMED / FETCH_FAILED
  matched_alias TEXT NOT NULL DEFAULT '',
  source_url TEXT NOT NULL DEFAULT '',
  page_title TEXT NOT NULL DEFAULT '',
  provider_context TEXT NOT NULL DEFAULT '',
  exclusion_context TEXT NOT NULL DEFAULT '',
  evidence_engine_version TEXT NOT NULL DEFAULT '',
  taxonomy_version TEXT NOT NULL DEFAULT '',
  researched_at TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(clinic_id, treatment_category_id)
);
```

- `clinic_id` は既存 `clinics.id`（legacy inner ID）をそのまま使う。医院名JOIN・電話番号JOINは禁止。
- `research_status` は上記4値のみ。**`NOT_RESEARCHED` はテーブルに行を作らないことで表現する**
  （その clinic_id × treatment_category_id の組が未調査、または対象外）。
- 1医院・1治療カテゴリにつき1行。複数治療カテゴリを持つ医院は複数行になってよい
  （Filter側は `EXISTS` で判定するため、一覧・CSVの1clinic=1行原則は崩れない）。
- Research対象はCrestix9カテゴリに限定しない（Research側方針変更に合わせ、Filter側もこの
  テーブルをNavi正式診療科・Crestix営業カテゴリと独立したJOIN軸として扱う）。
- 増分追従: Research側がこのテーブルに行を追加していくだけでよい。Filter側のコード変更は不要
  （`src/master/filters.py` は常にこのテーブルを都度クエリする）。

## Filter側の利用ルール

- 「HP治療カテゴリ」filterで治療カテゴリを指定した場合、`research_status='CONFIRMED'` の行のみ
  営業対象に含める。`REVIEW` / `NOT_CONFIRMED` / `FETCH_FAILED` / 行なし（`NOT_RESEARCHED`）は除外する。
- 「Research Status」filterは上記ルールとは独立した診療状況フィルタで、`CONFIRMED` /
  `REVIEW` / `NOT_CONFIRMED` / `FETCH_FAILED` / `NOT_RESEARCHED` を選択できる（同一医院に対する
  いずれかのtreatment_category行が該当statusを持てばヒット。`NOT_RESEARCHED` は該当clinic_idの行が
  1件もないことを意味する）。

## 変更履歴

- 2026-09-30: 初版。Filter/UI/CSV担当セッションが定義。
