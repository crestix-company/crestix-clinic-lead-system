# MHLW(医療情報ネット)診療科フィルター — 開発者向け再現手順

旧13,970件のClinic Masterに対して、厚生労働省 医療情報ネットの公式診療科を
安全な1:1 JOINとHP本人確認で紐付け、ナビイ正式診療科とCrestix営業カテゴリを
独立したfilterとして利用するためのパイプライン。

**Production DB（`./data/clinics.sqlite3`）には一切書き込まない。** このディレクトリの成果物は
正式採用版は `clinic_mhlw_departments_final.sqlite3` という独立したsidecar DBとしてATTACHされ、既存の
`clinics`/`uuid`/`medical_key`/`departments_json`/`treatments_json` 等は無変更のまま。

## Git管理区分

| 区分 | 内容 |
|---|---|
| **Git管理する（ソース）** | `*.py`（全スクリプト）、`crestix_department_mapping.py`（mapping定義）、`README.md`、`.gitignore` |
| **Git管理しない（生成物・`.gitignore`で除外済み）** | `*.sqlite3`（sidecar DB）、`*.csv`（Phase成果物）、`*.json`（サマリー）、`*.log` |

生成物はすべて下記コマンドで**決定的に**（同じ入力からは毎回バイト同一に）再生成できる。
巨大なMHLW元CSV（4ファイル、合計約280MB）もGit管理しない（`~/Downloads/`に別途配置する運用）。

## 正式採用値（2026-09-29）

- Clinic Master: 13,970医院
- FINAL_MATCHED: 9,830医院（70.37%）
- 診療科レコード: 28,117件
- ナビイ正式診療科名: 227種類
- REVIEW / 未MATCHED: 4,140医院

Rule C（住所のみ1:1一致）は自動MATCHしない。公式HPで施設固有の住所・電話を確認できたものだけを
昇格し、同一医療モール、同一法人の別施設、本院・分院等の可能性が残るものはREVIEWを維持する。

## データの役割

| データ | 役割 |
|---|---|
| 厚生局 | Clinic Masterの基礎データ |
| 厚生労働省 医療情報ネット（ナビイ） | 正式診療科（`mhlw_department_code` / `mhlw_department_name`） |
| Crestix mapping | 営業カテゴリ（`crestix_department`）。正式名称を上書きしない |
| Google Maps | 公式HP URL候補 |
| 公式HP | 施設本人確認。治療カテゴリは後続Phase 7でのみ調査 |

UIでは「ナビイ正式診療科」と「Crestix営業カテゴリ」を別filterとして扱う。各filter内はOR、
両方を指定した場合は医院単位でANDとなる。正式診療科は完全一致で、substring判定は禁止。

## 必要なMHLW CSV（固定4ファイル）

厚労省 医療情報ネット オープンデータより、以下4ファイルを固定パスに配置する。
同名ZIPの重複ファイル（`... 2.csv`等）は使用しない。

```
~/Downloads/02-1_clinic_facility_info_20260601.csv       # 一般診療所 施設票
~/Downloads/02-2_clinic_speciality_hours_20260601.csv    # 一般診療所 診療科・診療時間票
~/Downloads/03-1_dental_facility_info_20260601.csv       # 歯科診療所 施設票
~/Downloads/03-2_dental_speciality_hours_20260601.csv    # 歯科診療所 診療科・診療時間票
```

別PCで再現する場合は、各スクリプト冒頭の `FACILITY_FILES`/`SPECIALITY_FILES` のパスを
実際の配置場所に合わせて書き換える（現状は上記絶対パスをハードコードしている）。

## 生成手順（依存順）

```bash
# 1. MHLW施設×診療科を突合し、診療科マスタを作る（*991/*992自由記載も含む全行）
python3 mhlw_dry_run/phase2_build_department_master.py
#   -> mhlw_department_master.csv

# 2. MHLW診療科コード/名称 -> Crestix対象9科 のmapping表を作る
#    (mapping規則そのものは crestix_department_mapping.py にGit管理されたコードとして存在する。
#     このスクリプトはMHLW CSVを読んで実際に出現するcode/nameとmappingを突合するだけ)
python3 mhlw_dry_run/phase3_build_crestix_mapping.py
#   -> mhlw_to_crestix_department_mapping.csv, department_mapping_review.csv

# 3. 旧13,970件 x MHLW施設 を 医院名+住所 正規化で1:1確定JOINする(READ ONLY)
python3 mhlw_dry_run/phase4_join.py
#   -> legacy_mhlw_join.csv, join_summary.json

# 4. (任意・分析用) Crestix対象9科ごとの件数集計・HP調査候補抽出
python3 mhlw_dry_run/phase5_6.py
python3 mhlw_dry_run/phase6_5_hp_url_audit.py

# 5. 最終監査結果から正式採用sidecarを構築する
python3 mhlw_dry_run/finalize_mhlw_sidecar.py
#   -> clinic_mhlw_departments_final.sqlite3
```

旧 `clinic_mhlw_departments.sqlite3` はrollback用に残しており、即時削除しない。

## 出力先

すべて `mhlw_dry_run/` 直下（このディレクトリ）。sidecar DBのパスは
`src/master/store.py` の `MHLW_SIDECAR_PATH` 1箇所で管理され、正式版
`mhlw_dry_run/clinic_mhlw_departments_final.sqlite3` を参照する。

## sidecarのデプロイ注意点

`*.sqlite3` は `.gitignore` 対象なので、コードをpushしただけでは別PCやProductionにfinal sidecarは存在しない。
現構成では、固定した入力CSV・監査成果物からデプロイ工程で再生成し、件数・integrity・SHA等を検証してから
配置する **A: デプロイ時再生成** が最も追跡しやすく安全な第一候補。入力データの安全な配布が難しい場合は
**B: 署名・checksum付きrelease artifact配布** が次候補。**C: Git LFS** はリポジトリ更新とDB配布が密結合し、
誤更新や容量管理の負担が増えるため現時点では推奨しない。方式の最終決定・デプロイ実装は今回の範囲外。

## 再生成してもclinics.idが変わらない理由

`phase4_join.py` は `./data/clinics.sqlite3` を **`mode=ro` (`PRAGMA query_only=ON`)** で開き、
`SELECT id, uuid, clinic_name, address, medical_key FROM clinics` するだけで、
一切のINSERT/UPDATE/DELETEを行わない。出力CSVの `clinic_id` 列は既存の `clinics.id` を
そのままコピーしたものであり、新しいIDを採番するロジックはどこにも存在しない。
そのため、何度再生成しても・別PCで実行しても、同じ `clinics.sqlite3` に対しては
同じ `clinic_id` の組み合わせが得られる（DBのSHA-256は生成前後で完全一致することを
毎回`shasum -a 256 ./data/clinics.sqlite3`で確認している）。

## sidecarが存在しない場合のUI挙動

`src/master/store.py` の `ClinicStore.connect()` はfinal sidecarの存在と必須テーブルを確認し、
利用可能な場合のみ `ATTACH DATABASE ... AS mhlwdb` する。

`app_v2.py` の `filters_ui()`（詳細設定＞営業対象フィルター（詳細）で使用）は
`mhlw_sidecar_available()` で可用性を見て、

- **sidecarあり**: 「ナビイ正式診療科」と「Crestix営業カテゴリ」の2つの多重選択を表示
- **sidecarなし**: 2つの選択を非表示にし、既存filterが利用可能であることをcaption表示する

既存の「診療科」（`departments_json`ベース）・HP・都道府県などの他のfilterはsidecarの有無に
関係なく通常通り動作する。

MHLW関連filterをsidecar不在のままAPIから直接呼んだ場合は、生のSQLiteエラーではなく
`MhlwSidecarUnavailableError` で失敗する（結果を黙って空にしたり誤った件数を返したりしない）。
旧 `Filters.mhlw_departments` はCrestix営業カテゴリとしての後方互換のみ維持する。

## mapping定義のテスト

`crestix_department_mapping.py` の `classify()` は `tests/test_mhlw_crestix_mapping.py` で
回帰テストされている。特に「心療内科」「整形外科」「外科（一般外科）」がsubstring一致で
誤って対象科目に含まれないことを個別に固定している。

```bash
python3 -m pytest tests/test_mhlw_crestix_mapping.py -v
```

`TestClassifyLogic` はMHLW CSV/sidecar DBに依存せず常に実行される。`TestSidecarRegression` は
sidecar DBと `./data/clinics.sqlite3` が存在する場合のみ実行され（無ければ自動skip）、
final sidecarの9,830医院・28,117診療科レコード・正式名称227種類、および正式診療科/Crestix営業
カテゴリごとの採用件数を固定している。

## Phase 4.1 v2: JOIN精度改善(READ ONLY dry run・v1は無変更)

`phase4_join_v2.py` は既存(v1)の `legacy_mhlw_join.csv`(MATCHED 3,274件)を**削除・上書きせず**、
そのMATCHEDをRule A として踏襲したうえで、REVIEW/UNMATCHEDだった残り10,696件に対して
追加のRule B/C/Dを適用し、安全に1:1確定できるものだけを新たにMATCHEDへ引き上げる。

```bash
python3 mhlw_dry_run/phase4_join_v2.py
#   -> phase4_join_v2.csv, join_summary_v2.json,
#      clinic_mhlw_departments_v2.sqlite3, unique_address_match_audit.csv
```

- `normalize_v2.py`: `normalized_clinic_name`(法人種別を残す軽量正規化)・
  `comparison_clinic_name`(法人種別を保守的に除去。既存`normalize_clinic_name`が対応しない
  一般社団法人/公益社団法人/一般財団法人/公益財団法人は種別トークンのみ除去し、
  団体名の「...会」境界は推測しない)・`normalized_base_address`(ビル名・階数を除いた住所本体)
  を提供する純粋モジュール。既存 `src/normalizer/*` は変更していない。
- Rule B〜Dはすべて「両側(Clinic Master側・MHLW側)で候補1件のみ」の場合に限りMATCHEDとし、
  かつ **medical_type(医科/歯科)がRule A以外の全Ruleで一致することを必須**にしている
  (「日本橋休日応急診療所」(医科)が「日本橋休日応急歯科診療所」(歯科)に誤MATCHしそうになった
  実例を発見し、この種別ガードを追加した)。
- fuzzy score(Levenshtein/SequenceMatcher等)による自動確定は一切行っていない。
  `unique_address_match_audit.csv` の`name_similarity`列はRule C促進医院の**事後監査用**であり、
  MATCH判定そのものには使っていない。
- 既知の制限: `normalized_base_address`は「３０－２階」のように番地とフロア番号が
  ハイフンで地続きの場合、フロア番号がbaseに混入することがある(安全側の制限であり、
  baseがより限定的になるだけで誤結合のリスクは増えない)。

## Production配布時のsidecar配置について（今回は未実施の提案）

このsidecar DBは生成物のためGit管理しない方針だが、アプリ実行にはsidecarが無いと
MHLW filterが使えない。ローカル検証止まりの現段階では各自が上記手順で生成すればよいが、
複数PC・Production配布時は以下のいずれかを推奨する。

1. 固定入力と監査成果物を用意し、デプロイ工程で `finalize_mhlw_sidecar.py` を実行して再生成する。
2. 再生成入力を安全に配布できない場合は、checksum付きrelease artifactとして
   `clinic_mhlw_departments_final.sqlite3` をProduction DBとは別チャネルで配布する。
3. Git LFSはDB更新とコード更新が密結合するため、現時点では優先しない。

いずれもProduction migration自体は今回のスコープ外（ローカル検証のみ）であり、実施しない。
