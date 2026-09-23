# 全国 Google Maps 3台並列収集

この機能は、HP内容調査とは独立した「先行収集」用です。

- 全国の地方厚生（支）局が公開する 2026-09-01 時点の医科「コード内容別医療機関一覧」を取得します。
- 病院、名称に「センター」を含む施設、休止・廃止・辞退・取消を Maps 収集対象から除外します。
- `data/clinics.sqlite3` は読み取り専用で参照し、Maps 調査完了済み医院を除外します。
- ERROR / AMBIGUOUS は再試行対象に残します。
- Google Maps Clinic Collector v5.7.0 の既存8列と同じ形式で、PC1/PC2/PC3へほぼ同数に分割します。
- 3台のPCから同じSQLiteを共有・更新しません。各PCではCSVだけを使います。

## 起動

`02_start_national_maps_3pc.cmd` をダブルクリックするか、PowerShellから実行します。

ブラウザ: http://127.0.0.1:8502/

## 出力

`national_maps_output/<日時>/` に以下を保存します。

- `maps_queue_pc1.csv`
- `maps_queue_pc2.csv`
- `maps_queue_pc3.csv`
- `maps_queue_all.csv`
- `national_kouseikyoku_master.csv`
- `manifest.json`
- `README_FIRST.txt`

同じ内容をまとめた `national_maps_3pc_<日時>.zip` も作ります。

## 3台収集後

各PCの Collector 結果CSVをメインPCへ戻します。先に `national_kouseikyoku_master.csv` を厚生局マスターとして統合し、その後に3台のMaps結果を統合します。HP内容調査はその後に実行します。
