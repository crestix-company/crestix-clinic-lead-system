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

### ISリーダー向け正式手順

標準データフォルダーは次の場所です。3DBはGit管理せず、リポジトリ外へ配置します。

```text
%USERPROFILE%\CrestixData\clinic-lead\
  clinics.sqlite3
  treatment_research_final.sqlite3
  hp_abc_batch_sidecar.sqlite3
```

初回:

1. GitHubからリポジトリをcloneする。
2. 上記データフォルダーを作成する。
3. 3つのSQLiteスナップショットを配置する。
4. `setup_v2_windows.bat`をダブルクリックする。
5. 完了後、`start_v2_windows.bat`をダブルクリックする。

以後は`start_v2_windows.bat`だけで、未コミット変更がないことを確認し、`git pull --ff-only`で
GitHubのmainを最新版へ更新してから、3DBをREAD ONLYでpreflightし、アプリを1つ起動します。
不足DBがある場合は空DBを作成せず、ファイル名を日本語で表示して停止します。

個別パスを変更する場合、既存env varが`CLINIC_DATA_DIR`より優先されます。

```text
CLINIC_DB_PATH
TREATMENT_RESEARCH_DB_PATH
HP_RESEARCH_BATCH_DB_PATH
  ↓ 未設定の場合
CLINIC_DATA_DIR（未設定時は %USERPROFILE%\CrestixData\clinic-lead）
```

最新版へ手動更新する場合:

1. アプリを`Ctrl+C`で停止する。
2. `git status --short`で変更がないことを確認する。
3. `git switch main`を実行する。
4. `git pull --ff-only origin main`を実行する。
5. 必要な場合だけ、管理者から受領した3DBのsnapshotへ入れ替える。
6. `start_v2_windows.bat`をダブルクリックする。

禁止事項:

- `git reset --hard`、`git clean -fd`、`git push --force`を使用しない。
- Production DBやsidecarをリポジトリ内へ配置しない。
- OneDrive、Google Drive等で同一SQLiteを複数PCから同時writeしない。

コマンドで初回セットアップする場合:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe scripts\launch_v2.py
```

通常は上記コマンドではなく、BATファイルを使用してください。

## DB更新

旧DBを削除・初期化しません。初回2.1.0起動時に必要なMaps列・テーブルを後方互換で追加し、更新前DBバックアップを作成します。

## Production DB（重要）

**Production DBはリポジトリ内の`data/clinics.sqlite3`ではありません。** 詳細は`README.md`の「Production DB（重要）」を参照してください。起動前に環境変数`CLINIC_DB_PATH`でリポジトリ外の実DBを指定してください。

## Phase 7 治療カテゴリResearch方針

治療カテゴリの正式な定義は[`config/treatment_taxonomy.yml`](config/treatment_taxonomy.yml)（`7A-v2`）に集約しています。ナビイ診療科は診療科の正データ、Crestix営業カテゴリは営業上の分類、Treatment Categoryは公式医院HPで提供を確認する別データです。疾患・専門領域は`clinical_focus`として分け、診療科や医院名から治療の提供を推測しません。`7A-v1`は互換snapshotとして残し、Phase 7-B Research対象は`ACTIVE`だけです。

`CONFIRMED`には公式HP上で医院自身が提供を明示した根拠を必要とします。否定・紹介・一般解説・文脈不明は確定せず、`REVIEW`または`NOT_CONFIRMED`とします。`NOT_CONFIRMED`は「HPから確認できなかった」という意味で、未提供を意味しません。ソース判定には既存`is_official_candidate()`を使います。Evidenceの保存schemaもtaxonomy内に定義していますが、Phase 7-AではProduction DBへ書き込みません。

Phase 7-BはACTIVE 43 Treatment Categoryごとに陽性候補2件・陰性候補4件を最低限含めて層化し、clinic ID重複を除いた約270医院（許容範囲200〜300）を対象とする設計です。HP本人確認状態、Maps公式HP URL、ナビイ診療科一致、複数診療科、大規模・小規模HPを混在させ、約30医院を人手監査します。既存カテゴリ有無はあくまでサンプリング層で、正解ラベルにはしません。precision、REVIEW/NOT_CONFIRMED率、alias別ヒット、誤検知と否定文誤検知、取得成功率、処理時間、HTTP request数を計測します。現時点でHTTPアクセス・Phase 7-B Researchは未開始です。Phase 7-B結果の確認前にPhase 7-Cへ進みません。
