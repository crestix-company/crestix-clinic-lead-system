全国 Google Maps 3台並列収集パッケージ

1. PC1には maps_queue_pc1.csv、PC2には maps_queue_pc2.csv、PC3には maps_queue_pc3.csv を渡します。
2. 各PCでは Google Maps Clinic Collector v5.7.0 だけを実行してください。clinics.sqlite3 は共有・コピー・書込みしません。
3. 各PCの結果CSVは上書きせず、PC番号が分かる名前で保存してください。
4. 3台完了後、結果CSV3つと national_kouseikyoku_master.csv をメインPCに戻します。
5. 厚生局マスターを先に統合し、その後Maps結果を統合します。

このキュー作成処理は本番SQLiteを読み取り専用で参照し、既にMaps調査完了済みの医院を除外しています。
