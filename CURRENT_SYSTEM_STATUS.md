# CURRENT SYSTEM STATUS

Last updated: 2026-10-05

このファイルは、`crestix-clinic-lead-system` の現在地を共有するための運用メモです。営業利用・別PC引継ぎ時は、まずこのファイルと `README.md` を確認してください。

## 1. 正式な運用基準

- Repository: `crestix-company/crestix-clinic-lead-system`
- Production code: `main`
- Production DB: `$HOME/CrestixData/clinic-lead/clinics.sqlite3`
- Treatment sidecar: `$HOME/CrestixData/clinic-lead/treatment_research_final.sqlite3`
- Production DB / sidecar は Git 管理外
- 別PCでは GitHub clone だけでは実データは取得できないため、Productionデータを別途安全に配置する

## 2. Clinic Master / UUID

- Clinic Master: **162,258 clinics**
- Comdesk distinct UUID source: **24,955**
- Current Production linked UUID: **15,740**
- Current UUID coverage: 約 **63%**

### Phase3 DUAL_EXACT candidate

- Input: 5,448
- Eligible: **5,436**
- Hard-guard reject: 12
- Human Audit sample: 300
- Projected linked after approved apply: **21,176**
- Projected coverage: **84.86%**

ただし、Phase3 Human Audit 300件は現時点で未入力です。

`artifacts/comdesk_initial_sync/phase3_dual_exact/human_audit_300.csv`

最新audit結果:

- row_count = 300 / 300 PASS
- candidateとの非label項目 mismatch = 0
- duplicate UUID = 0
- duplicate clinic_id = 0
- Production DB read-only reverify = PASS
- audit_label: CORRECT 0 / INCORRECT 0 / UNCERTAIN 0 / blank 300
- Status: **HUMAN_REVIEW_REQUIRED**
- 5,436件のProduction Applyは未実施

したがって、**現在正式に営業利用するUUIDは15,740件時点**です。

## 3. HP / Sales classification

現行正式分類:

- A VERIFIED: **1,313**
- B LIKELY: **1,686**
- C SPECIALTY: **4,593**
- D UNKNOWN: **1,505**
- Sales usable (A+B+C): **7,592**

運用ルール:

- A: 治療実施を確認済みとして扱える
- B: likely。営業で治療実施を断定しない
- C: 診療科 / specialtyベース
- D: 通常の営業抽出対象外

v2 candidateは別artifactで作成中。既存final artifactは上書きしない。

## 4. 診療科 / Treatment

- 正式診療科: MHLW / ナビイ系データを基準
- Crestix営業診療科: 正式診療科とは別軸
- Treatment: 公式HP上の提供根拠を基準
- HYBRID Treatment Research v1 は main へ反映済み
- ambiguous / cross-department treatment alias guard も main へ反映済み

診療科・治療カテゴリ・Sales Tier・Confidence等で営業対象をfilter可能。

## 5. Comdesk CSV

営業利用可能。

通常のComdesk正式出力は固定28列:

`UUID,種別,名前,カナ,郵便番号,都道府県,住所１,住所２,住所カナ,Tel1,Tel2,Tel3,Tel4,FAX,URL,備考,旧社名,リードソース,履歴,記事名,休診日,診療日,午前始,午前終,午後始,午後終,院長名,開業日`

抽出時は、最新Production DBからUUIDをJOINする。

例:

```bash
cd ~/Desktop/clinic-list-filter-complete
python3 -m scripts.export_sales_list_to_comdesk --interactive
```

現時点では **15,740 linked UUID時点**の最新Productionを使ってCSVを生成可能。

## 6. 今すぐ営業利用する場合

Phase3 UUID監査完了を待たず、現在の15,740 linked UUIDを使用して営業リストを作成してよい。

手順:

1. `main`を最新化
2. Production DBを指定
3. 診療科 / Treatment / Tier等を選択
4. previewで件数確認
5. 最新Production UUIDをJOINしてCSV生成
6. duplicate UUID / clinic_idを確認
7. Comdeskへ投入

Phase3 5,436件がHuman Audit後にProductionへ反映された場合は、Comdesk投入前にCSVを再生成する。

## 7. 別PC / ISリーダー運用

推奨構成:

- 前川PC: Master / 開発 / Production更新
- ISリーダーPC: read-only利用 / filter / CSV抽出 / Comdesk投入

SQLiteでは、複数PCから同一Production DBへ同時WRITEしない。

ISリーダーPCでは:

```bash
git clone https://github.com/crestix-company/crestix-clinic-lead-system.git ~/Desktop/clinic-list-filter-complete
cd ~/Desktop/clinic-list-filter-complete
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements-lock.txt
```

その後、Production DB / Treatment sidecar / 必要なMHLW sidecarを安全な経路で別途配置する。

## 8. HP Rank

Open PR #11 / #16 / #17 / #18 / #19 / #20 はHP Rank実験チェーン。

- Production未採用
- mainの営業抽出をブロックしない
- 個別に全PRをmainへmergeしない
- 本番採用時に最新mainへ必要コードだけ統合したProduction PRを別途作成する

## 9. 現在の最優先残作業

1. Phase3 DUAL_EXACT Human Audit 300件
2. 5,436件のGO判定 / Production Apply
3. HP generic alias cleanup
4. D UNKNOWN rescue / v2 candidate確定
5. 最新UUIDでComdesk CSV再生成

## 10. 現在の利用可否

**営業利用: GO**

条件:

- 現時点の正式UUIDは15,740件を使用
- Phase3 5,436件はHuman Audit完了まで未反映
- Production DBは最新を参照
- D UNKNOWNは通常出力しない
- UUID / clinic_idの重複チェックを維持

この条件で、診療科・治療カテゴリ別の営業リスト抽出とComdesk CSV生成を開始して問題ありません。
