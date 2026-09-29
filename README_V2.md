# クリニック営業マスター 2.1.0

厚生局を母集団に、既存Comdesk・Google Maps収集結果・HP解析結果を1医院=1マスターへ統合するローカルStreamlitアプリです。既存SQLite、UUID、Comdesk元行、履歴、手動修正、調査結果を保持したまま更新できます。

## 全体フロー

1. **既存Comdeskを取り込む**（最新UUID・履歴付き）
2. **厚生局データを統合する**（全件を内部マスターへ保持）
3. **Google Maps調査キューCSVを出力する**
4. Chrome拡張 `Google Maps Clinic Collector 5.7.0` で1医院ずつMaps本人確認・ウェブサイトURLを収集する
5. **Google Maps取得結果を取り込む**
6. Google MapsウェブサイトURLがある医院はそのURLを第一候補としてHP内容を調査する。HP発見のためのTavily検索は行わない
7. 治療、HPランク、広告施策、年齢、昼の検査・手術専用枠などを既存ロジックで判定する
8. 営業対象フィルターで絞り、**Comdesk A〜ABの28列固定**で出力する

## Google Mapsデータ

「マスター管理」から次を行えます。

- `Google Maps調査キューCSVをダウンロード`
- `Google Maps取得結果を取り込む`
- `管理用フルCSVをダウンロード`

Google Maps結果は `internal_clinic_id` → 医療機関番号 → `tel_match_key` → 医院名+住所の順で既存マスターに紐付けます。同じ結果CSVを再取込しても二重登録しません。

Google Maps上で本人確認できた医院は `MAPS_MATCHED_WEBSITE` または `MAPS_MATCHED_NO_WEBSITE` として保持し、未発見・要確認・病院/センター除外・エラーを別状態で保持します。

## Comdesk標準出力

通常ダウンロードCSV/Excelは以下の**28列だけ**です。内部clinic ID、医療機関番号、MapsプロフィールURL、照合状態、スコア等は通常出力へ追加しません。

```text
UUID,種別,名前,カナ,郵便番号,都道府県,住所１,住所２,住所カナ,Tel1,Tel2,Tel3,Tel4,FAX,URL,備考,旧社名,リードソース,履歴,記事名,休診日,診療日,午前始,午前終,午後始,午後終,院長名,開業日
```

既存Comdesk元行はそのまま保持します。既存URLが入っている場合、Google Mapsの別URLへ自動上書きしません。既存URLが空欄で、Google Mapsで本人確認済みのウェブサイトURLがある場合だけURL列を補完します。新規医院はUUID空欄です。

## 病院・センター

厚生局マスターからは削除しません。Google Maps調査キューでは「病院」「センター」を検索前除外として結果へ記録し、通常の営業用Comdesk出力には含めません。

## Windows

初回:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe scripts\launch_v2.py
```

または `setup_v2_windows.bat` → `start_v2_windows.bat` を使用します。2回目以降は `start_v2_windows.bat` だけで起動できます。

## DB更新

旧DBを削除・初期化しません。初回2.1.0起動時に必要なMaps列・テーブルを後方互換で追加し、更新前DBバックアップを作成します。

## Production DB（重要）

**Production DBはリポジトリ内の`data/clinics.sqlite3`ではありません。** 詳細は`README.md`の「Production DB（重要）」を参照してください。起動前に環境変数`CLINIC_DB_PATH`でリポジトリ外の実DBを指定してください。

## Phase 7 治療カテゴリResearch方針

治療カテゴリの正式な定義は[`config/treatment_taxonomy.yml`](config/treatment_taxonomy.yml)（`7A-v2`）に集約しています。ナビイ診療科は診療科の正データ、Crestix営業カテゴリは営業上の分類、Treatment Categoryは公式医院HPで提供を確認する別データです。疾患・専門領域は`clinical_focus`として分け、診療科や医院名から治療の提供を推測しません。`7A-v1`は互換snapshotとして残し、Phase 7-B Research対象は`ACTIVE`だけです。

`CONFIRMED`には公式HP上で医院自身が提供を明示した根拠を必要とします。否定・紹介・一般解説・文脈不明は確定せず、`REVIEW`または`NOT_CONFIRMED`とします。`NOT_CONFIRMED`は「HPから確認できなかった」という意味で、未提供を意味しません。ソース判定には既存`is_official_candidate()`を使います。Evidenceの保存schemaもtaxonomy内に定義していますが、Phase 7-AではProduction DBへ書き込みません。

Phase 7-BはACTIVE 43 Treatment Categoryごとに陽性候補2件・陰性候補4件を最低限含めて層化し、clinic ID重複を除いた約270医院（許容範囲200〜300）を対象とする設計です。HP本人確認状態、Maps公式HP URL、ナビイ診療科一致、複数診療科、大規模・小規模HPを混在させ、約30医院を人手監査します。既存カテゴリ有無はあくまでサンプリング層で、正解ラベルにはしません。precision、REVIEW/NOT_CONFIRMED率、alias別ヒット、誤検知と否定文誤検知、取得成功率、処理時間、HTTP request数を計測します。現時点でHTTPアクセス・Phase 7-B Researchは未開始です。Phase 7-B結果の確認前にPhase 7-Cへ進みません。
