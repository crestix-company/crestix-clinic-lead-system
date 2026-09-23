# CODEX HANDOFF MINIMAL — clinic-list-filter-complete 2.1.0

## 現在の完成設計

システムは2アプリです。

1. `google-maps-clinic-collector-v5.7.0`: 厚生局母集団CSVを受け、医院名+電話番号（失敗時のみ医院名+住所）でGoogle Mapsを1医院ずつ本人確認し、Business Profileの「ウェブサイト」欄を収集する。Google Places/SerpAPI/Tavilyはこの工程に使わない。
2. `clinic-list-filter-complete 2.1.0`: 厚生局 + Google Maps結果 + 既存Comdesk + HP解析等を内部マスターへ統合し、通常出力はComdesk A〜AB 28列だけにする。

## 絶対に維持するもの

- 既存SQLiteを削除/初期化しない
- 既存UUID、Comdesk元行、履歴、手動修正、調査結果を保持
- `tel_match_key` の既存電話照合仕様を維持
- 異なる既存UUIDを自動統合しない
- 厚生局全件マスターを保持
- HPランク、年齢推定、継承、治療カテゴリ、広告施策、EPARK、昼の検査・手術専用枠を維持

## Google Maps統合

Maps結果取込の紐付け優先順は:

1. `internal_clinic_id`
2. 医療機関番号
3. `tel_match_key`
4. 医院名+住所

Maps状態は内部に保存する。`MAPS_MATCHED_WEBSITE` / `MAPS_MATCHED_NO_WEBSITE` / `MAPS_NOT_FOUND` / `MAPS_AMBIGUOUS` / `EXCLUDED_HOSPITAL` / `EXCLUDED_CENTER` / `ERROR` を区別する。

Maps website URLが確認済みならHP discoveryのTavily検索を消費しない。URLを直接クロールし本人確認・内容解析する。

## Comdesk出力

通常CSV/Excelは次の28列固定。29列目は追加しない。

```text
UUID,種別,名前,カナ,郵便番号,都道府県,住所１,住所２,住所カナ,Tel1,Tel2,Tel3,Tel4,FAX,URL,備考,旧社名,リードソース,履歴,記事名,休診日,診療日,午前始,午前終,午後始,午後終,院長名,開業日
```

既存Comdesk行は保持。既存URLありならMaps URLで上書きしない。既存URL空欄のみ、本人確認済みMaps websiteでURLを補完できる。新規UUIDは空欄。内部項目は管理用フルCSVだけに出す。

## 2.1.0で追加した主な実装

- SQLite schema v4 / `google_maps_results`
- Maps projected fields
- 厚生局ベースのMaps調査キューCSV
- Maps結果のidempotent取込
- Maps掲載確認フィルター
- 管理用フルCSV
- Maps website優先HP解析（Maps URLあり時はHP discovery検索APIを呼ばない）
- 既存URL保護 + 空欄URLのみMaps補完
- 病院/センターを通常営業出力から除外

## 検証記録

この作業環境ではStreamlitパッケージをネットワーク制限のため追加インストールできなかったため、`tests/test_app.py` と `tests/test_v2_app.py` のStreamlit UIテストは収集不能。それ以外は実行すること。実Google Maps Chromeテストはこの環境では未実施と明示すること。
