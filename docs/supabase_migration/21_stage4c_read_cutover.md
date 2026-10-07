# Stage4-C Supabase READ Cutover

Status: **PASS**

Stage4-D WRITE Cutover: **GO（未着手）**

Recovery point: `f0040362ce979015569dc592ba2cd7d25cf8e831`

## Runtime architecture

- `CLINIC_DATA_BACKEND=supabase`（未指定時もStage4-C既定）: 検証済みREADはSupabase Primary。
- `CLINIC_DATA_BACKEND=sqlite`: 即時rollback。従来の`ClinicStore`をそのまま返す。
- WRITEは常に元のSQLite `ClinicStore`。Supabase adapterにruntime WRITEはない。
- `CLINIC_READ_COMPARATOR_ENABLED=1`（既定）: Supabase成功値を返した後、SQLite比較をbounded background workerで実行する。
- Supabaseのconnection error、timeout、query error、未対応READはSQLiteへfallbackし、`operation`、累積`fallback_count`、分類済み`fallback_reason`を`logs/read_cutover.log`へ記録する。接続障害後は次回READで再接続する。

Supabase Primary対象はclinic get、UUID/medical_key lookup、query、count、funnel、dashboard metrics、effective HP rank、site type、Treatment status for ids、HP batch metricsである。Treatment confirmed categoriesはrepository parity対象としてSupabase実装済みである。

`public.clinic_effective_hp_rank_v`はPython canonicalと11件不一致のため使用していない。Stage4-B.7で全162,258件一致したrepository内candidate SQLだけを使用する。keywordはASCII fold＋trigram indexを維持し、`ILIKE`は使用しない。

## Compatibility fallback / 未移行READ

現行UIから利用される次のfilterは機能を削除せず、Stage4-Cでは明示的SQLite compatibility fallbackとする。

- `mhlw_official_departments` / `crestix_sales_departments` / legacy `mhlw_departments`
- `sales_tiers` / `sales_confidence` / `exclude_human_review`
- `hp_treatment_categories` / `research_status` / `sales_pairs`
- `treatment_status_counts`（現行UI呼出しなし。adapter methodは未対応）

## Local SQLite / sidecar runtime dependencies（Stage5 removal target）

| Path / env | File / function | Stage4-C reason |
|---|---|---|
| `CLINIC_DB_PATH` | `app_v2.store_for`, `ClinicStore` delegated methods | 全WRITE、rollback、comparator、未移行READ、UI option query、export/revision |
| `TREATMENT_RESEARCH_DB_PATH` | `research_sidecar.py`, `ClinicStore.query/count`, UI option availability | Treatment filters、research status、sales pairs compatibility fallback |
| `HP_RESEARCH_BATCH_DB_PATH` | `hp_effective_rank.py`, `hp_site_type.py`, `hp_batch_metrics.py` | SQLite comparator/fallbackとWRITE-side workflow |
| MHLW final sidecar | `store.py:mhlw_sidecar_available`, MHLW filter helpers | ナビイ正式診療科/Crestix営業カテゴリ filter |
| Sales classification artifact | `sales_classification.py` | Sales tier/confidence/human-review filterとUI参考表示 |

`app_v2`内の`store.connect()`は都道府県・市区町村・maps availability・広告施策数などのUI選択肢生成に残る。データ一覧/countのPrimary結果ではないが、Stage5でrepository option APIsへ移す対象である。

## Verification

- 46/46 parity PASS。
- 325/325 shadow/comparator PASS、mismatch 0。
- full pytest: 1004 passed / 44 skipped / 0 failed。
- failure injection: connection、statement timeout、query exception、unsupported pathでSQLite fallback PASS。WRITE delegationはSQLiteのみ。
- UI（Supabase Primary）: 営業対象997、Comdesk 997、UUIDあり515、UUIDなし482、眼科68、keyword「クリニック」747、pagination page 2 PASS。
- warm 30-run p95: dashboard 189.00ms、営業対象124.30ms、filter眼科40.45ms、pagination141.83ms、keyword 462.66ms、HP A+B 143.42ms。全て通常UI 1秒以内。
- Production SHA: clinics `fc53c9de...8a0`、Treatment `36dbe570...d804`、HP `3980865e...7d53`（期待値一致）。
- Supabase: `public.clinics=162258`、19 migration tables total `632903`、row data write 0。
- Stage4-CではSupabase schema changeなし。Security AdvisorはStage4-B.7のERROR 0 / WARNING 0から変更要因なし。
