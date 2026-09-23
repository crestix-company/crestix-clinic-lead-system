# Google Maps Clinic Collector 5.7.0

厚生局/クリニックマスターCSVを母集団として、各医院を **医院名＋電話番号** でGoogle Maps検索し、本人確認後にGoogle Business Profileの **「ウェブサイト」欄** を取得するChrome拡張機能です。

## 重要仕様
- Google Places API / SerpAPI / Tavily はGoogle Maps検索・HP発見に使用しません。
- 検索前に「病院」「センター」を除外し、結果CSVには除外理由を残します。
- 電話番号一致を最優先。失敗時のみ医院名＋住所で救済します。
- Maps店舗URL、予約、メニュー等をHP URLとして代用しません。
- 誤紐付けが疑われる場合は `MAPS_AMBIGUOUS` とし、HP URLを採用しません。
- CAPTCHA/アクセス制限を回避しません。検知時はERRORとして保存し一時停止します。
- 1医院処理ごとにChrome storageへ保存。一時停止・再開・途中CSV保存ができます。

## 入力CSV
最低限、医院名と電話番号が必要です。次の列名を自動認識します。
- 医院名: `clinic_name` / `医院名` / `クリニック名` / `名前` / `医療機関名`
- 電話番号: `phone` / `電話番号` / `Tel1` / `電話`
- 推奨: `internal_clinic_id` / `clinic_id`, `medical_institution_number` / `医療機関番号`, `住所`, `都道府県`, `施設区分`

## 出力CSV
これはComdesk用ではなく、マスター管理アプリへ渡す収集結果CSVです。内部clinic ID、医療機関番号、元医院名・電話・住所、Maps本人確認結果、MapsプロフィールURL、MapsウェブサイトURL、照合方法、診療時間原文等を保持します。

## 実Google Mapsテスト
この成果物を作成した環境からChrome実画面を操作していないため、実Google Mapsテスト済みとはしていません。最初に5〜10医院で、電話一致・プロフィール・ウェブサイト欄・前医院HP混入なし・一時停止/再開を確認してください。

### 収集結果CSVの固定列

```text
internal_clinic_id,medical_institution_number,source_clinic_name,source_phone,source_address,source_prefecture,maps_match_status,maps_match_method,maps_name,maps_phone,maps_address,maps_profile_url,maps_website_url,website_status,phone_match,name_match,address_match,exclude_reason,scrape_status,scraped_at,休診日,診療日,午前始,午前終,午後始,午後終,営業時間原文
```

このCSVはComdeskへ直接入れません。`clinic-list-filter-complete 2.1.0` の「Google Maps取得結果を取り込む」へ渡します。
