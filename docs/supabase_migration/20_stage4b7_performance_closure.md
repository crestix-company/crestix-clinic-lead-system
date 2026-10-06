# Stage4-B.7 Targeted Performance Closure

Status: **PASS**

Stage4-C READ Cutover gate: **GO**（Stage4-C自体は未開始）

## 固定結果

- Keyword parity: 127 / 127 PASS、mismatch 0
- `filter_keyword`: p95 456.48ms
- Effective HP Rank candidate SQL: Python canonicalとの全162,258件比較でmismatch 0
- `filter_hp_rank_A+B`: p95 457.38ms
- Performance Budget: 13 / 13 PASS
- Parity: 46 / 46 PASS
- Shadow: 325 / 325 PASS、mismatch 0
- pytest: 996 passed / 44 skipped / 0 failed
- Supabase row data writes: 0

## Keyword schema

Live Supabaseのcatalogで次を確認した。

- `pg_trgm` 1.6、schema `public`
- `idx_clinics_name_ascii_fold_trgm`: valid / ready
- `idx_clinics_phone_ascii_fold_trgm`: valid / ready

再現用DDLは
[`stage4b7_keyword_search_indexes.sql`](../../scripts/supabase_migration/stage4b7_keyword_search_indexes.sql)
に固定した。検証後にDROPしたHP rank indexは含めない。

## Known residuals

### 2文字keyword

2文字語「東京」は`pg_trgm`を利用できず、30 warm runsでp95 864.3msだった。
定義済み13 Performance Budgetは13 / 13 PASSであり、UI全体の許容基準内のため、
Stage4-C blockerにはしない。ただし短いkeywordの既知performance edge caseとして残す。

### Effective HP Rank View

`src/master/hp_effective_rank.py`の`effective_hp_rank()`をcanonical SSOTとする。
`public.clinic_effective_hp_rank_v`は全162,258件比較で11件不一致だった
（`fetch_status='ERROR'`、machine rank空欄、legacy Aが6件・Bが5件）。

したがって、**Stage4-C runtimeで`public.clinic_effective_hp_rank_v`を利用してはならない**。
Repository内の全件mismatch 0を確認済みcandidate SQLを使用する。
