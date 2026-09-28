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

**Production DBはリポジトリ内の`data/clinics.sqlite3`ではありません。** 可変データをGit管理すると、`git switch`/`restore`/`reset`/古いbranchへの移動などでDBが巻き戻る・削除される事故が起きるため、Production DBはリポジトリ外に置いて運用します。

**リポジトリをclone/pullしただけではProduction DBは含まれません。** 起動には環境変数`CLINIC_DB_PATH`でリポジトリ外の実DBを明示的に指定する必要があります。`CLINIC_DB_PATH`が未設定、指定先が存在しない、SQLiteとして開けない、`clinics`テーブルが無い場合はアプリはエラーを表示して起動を停止します（**空DBの自動生成やrepo内の古いDBへの自動fallbackは一切しません**）。

```powershell
set CLINIC_DB_PATH=C:\path\to\external\clinics.sqlite3
.\.venv\Scripts\python.exe scripts\launch_v2.py
```

```bash
export CLINIC_DB_PATH=~/CrestixData/clinic-lead/clinics.sqlite3
.venv/bin/python scripts/launch_v2.py
```

Macでは`scripts/launch_v2_mac.sh`を使うと、`CLINIC_DB_PATH`未設定時に既定の配置場所（`$HOME/CrestixData/clinic-lead/clinics.sqlite3`。ユーザー固有の絶対パスをリポジトリにハードコードしないよう`$HOME`基準にしています）を使い、起動前にDBの存在・`clinics`テーブルの有無を確認してから起動します。

```bash
./scripts/launch_v2_mac.sh
```

古いbranch（`data/clinics.sqlite3`がGit LFSで追跡されていた履歴）へ切り替えても、`CLINIC_DB_PATH`を設定していればProduction DBそのものには影響しません。

`start_v2_windows.bat`をダブルクリックして起動する運用の場合は、事前に`CLINIC_DB_PATH`をWindowsのシステム環境変数として設定しておくか、ローカルの`start_v2_windows.bat`に`set CLINIC_DB_PATH=...`の行を追加してください（このファイル自体はGit管理下のため、パスを書き込む場合は各PCでの手元編集にとどめ、リポジトリへcommitしないでください）。Windowsでも、Production DBは各PCのローカルデータ領域（例：`C:\Users\<ユーザー名>\CrestixData\clinic-lead\clinics.sqlite3`）に配置し、`CLINIC_DB_PATH`で指定してください。

### 複数PC運用について

現在のSQLite構成では、**複数PCから同じProduction DBファイルへ同時に書き込む運用はできません**（Google Drive/Dropbox/OneDrive等での共有・同期を含みます）。PCごとに別々のコピーを持たせて同時更新することも禁止です。複数PCから同一データを継続的に更新する必要が生じた段階で、Central PostgreSQL / Supabase等への移行を検討してください（現時点では未実施・未計画です）。

## Google Maps Clinic Collector

Google Maps公式HP取得にはChrome拡張
`extensions/google-maps-clinic-collector/google-maps-clinic-collector`
を使用します。

Current version: v5.7.0

複数PC運用は
`extensions/google-maps-clinic-collector/MULTI_PC_SETUP.md`
を参照してください。
