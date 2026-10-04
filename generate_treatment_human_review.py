from pathlib import Path
import sqlite3
import csv

ROOT = Path.cwd()
PROD = Path.home() / "CrestixData/clinic-lead/clinics.sqlite3"
MHLW = ROOT / "mhlw_dry_run/clinic_mhlw_departments_final.sqlite3"
RESEARCH = Path.home() / "CrestixData/clinic-lead/treatment_research_final.sqlite3"
OUTDIR = Path.home() / "Downloads"

for p in (PROD, MHLW, RESEARCH):
    if not p.exists():
        raise SystemExit(f"ERROR: ファイルがありません: {p}")

conn = sqlite3.connect(f"file:{PROD}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

conn.execute(
    "ATTACH DATABASE ? AS mhlwdb",
    (f"file:{MHLW}?mode=ro",)
)
conn.execute(
    "ATTACH DATABASE ? AS researchdb",
    (f"file:{RESEARCH}?mode=ro",)
)

sql = """
WITH mhlw_distinct AS (
  SELECT DISTINCT clinic_id, mhlw_department_name
  FROM mhlwdb.clinic_mhlw_departments_final
  WHERE mhlw_department_name <> ''
),
mhlw_agg AS (
  SELECT clinic_id,
         GROUP_CONCAT(mhlw_department_name, ' / ') AS official_departments
  FROM mhlw_distinct
  GROUP BY clinic_id
),
eligible AS (
  SELECT
    c.id AS clinic_id,
    c.clinic_name,
    c.phone,
    c.address,
    COALESCE(c.maps_website_url, '') AS maps_website_url,
    COALESCE(c.hp_url, '') AS hp_url,
    m.official_departments
  FROM clinics c
  JOIN mhlw_agg m ON m.clinic_id = c.id
  WHERE c.merged_into IS NULL
    AND c.merge_hold = 0
    AND c.active = 1
    AND c.uuid = ''
    AND c.first_seen_at < '2026-09-28T02:26:53.465716+00:00'
    AND NOT (
      c.exclude_reason IN ('hospital','center')
      OR COALESCE(json_extract(c.effective_json,'$.facility_type'),'') = '病院'
      OR c.clinic_name LIKE '%病院%'
      OR c.clinic_name LIKE '%センター%'
    )
    AND EXISTS (
      SELECT 1
      FROM researchdb.clinic_treatment_research_final rr
      WHERE rr.clinic_id = c.id
    )
)
SELECT
  e.clinic_id,
  e.clinic_name AS 医院名,
  e.phone AS 電話番号,
  e.address AS 住所,
  e.official_departments AS 正式診療科,
  r.treatment_category_name AS 治療カテゴリ,
  r.research_status AS 現在判定,
  r.source_url AS 根拠URL,
  r.page_title AS ページタイトル,
  r.matched_alias AS 検出キーワード,
  r.provider_context AS 根拠文脈,
  r.exclusion_context AS 除外文脈,
  e.maps_website_url AS GoogleMaps_HP,
  e.hp_url AS 旧HP_URL,
  r.researched_at AS 調査日時
FROM eligible e
JOIN researchdb.clinic_treatment_research_final r
  ON r.clinic_id = e.clinic_id
WHERE r.research_status IN ('REVIEW','NOT_CONFIRMED')
ORDER BY
  CASE r.research_status WHEN 'REVIEW' THEN 1 ELSE 2 END,
  e.clinic_id,
  r.treatment_category_name
"""

rows = [dict(r) for r in conn.execute(sql)]
conn.close()

headers = [
    "clinic_id",
    "医院名",
    "電話番号",
    "住所",
    "正式診療科",
    "治療カテゴリ",
    "現在判定",
    "根拠URL",
    "ページタイトル",
    "検出キーワード",
    "根拠文脈",
    "除外文脈",
    "GoogleMaps_HP",
    "旧HP_URL",
    "調査日時",
    "人間判定",
    "確認メモ",
]

for row in rows:
    row["人間判定"] = ""
    row["確認メモ"] = ""

all_path = OUTDIR / "treatment_human_review_all.csv"
first50_path = OUTDIR / "treatment_human_review_first50.csv"

def write_csv(path, data):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(data)

write_csv(all_path, rows)
write_csv(first50_path, rows[:50])

review_count = sum(1 for r in rows if r["現在判定"] == "REVIEW")
not_count = sum(1 for r in rows if r["現在判定"] == "NOT_CONFIRMED")
clinic_count = len({r["clinic_id"] for r in rows})

print("")
print("=== PASS ===")
print(f"人間確認Treatment行: {len(rows):,}")
print(f"対象医院数: {clinic_count:,}")
print(f"REVIEW行: {review_count:,}")
print(f"NOT_CONFIRMED行: {not_count:,}")
print("")
print(f"全件: {all_path}")
print(f"最初の50行: {first50_path}")
print("")
print("Production DB / sidecar は read-only です。")
