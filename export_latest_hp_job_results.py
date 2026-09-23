import sqlite3, json, csv
from pathlib import Path

DB = Path(r"data\clinics.sqlite3")
OUT = Path("latest_hp_job_results.csv")

con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row

job = con.execute("""
SELECT id, created_at, status
FROM research_jobs
WHERE kind='hp'
ORDER BY rowid DESC
LIMIT 1
""").fetchone()

if job is None:
    print("HP調査ジョブが見つかりません。")
    raise SystemExit(1)

rows = con.execute("""
SELECT
    i.clinic_id,
    c.clinic_name,
    c.maps_website_url,
    i.status AS item_status,
    i.error_message,
    r.result_json
FROM research_job_items i
JOIN clinics c ON c.id = i.clinic_id
LEFT JOIN research_results r
  ON r.rowid = (
      SELECT rr.rowid
      FROM research_results rr
      WHERE rr.clinic_id = i.clinic_id
      ORDER BY rr.rowid DESC
      LIMIT 1
  )
WHERE i.job_id = ?
ORDER BY i.rowid
""", (job["id"],)).fetchall()

headers = [
    "clinic_id","医院名","Google Maps HP","ジョブ状態","ジョブエラー",
    "research_status","hp_status","HPランク","HPスコア","治療カテゴリ",
    "59歳以下確率","集客シグナル数","集客シグナル","アツさ",
    "昼の検査・手術枠","昼枠曜日","crawl_errors","research_error","error"
]

with OUT.open("w", newline="", encoding="utf-8-sig") as f:
    w = csv.writer(f)
    w.writerow(headers)
    for row in rows:
        try:
            d = json.loads(row["result_json"] or "{}")
        except Exception:
            d = {}

        cats = d.get("treatment_categories") or []
        if isinstance(cats, list):
            cats = " / ".join(str(x) for x in cats)

        sigs = d.get("marketing_signals") or []
        if isinstance(sigs, list):
            sigs = " / ".join(str(x) for x in sigs)

        crawl = d.get("crawl_errors") or []
        if not isinstance(crawl, str):
            crawl = json.dumps(crawl, ensure_ascii=False)

        w.writerow([
            row["clinic_id"],
            row["clinic_name"],
            row["maps_website_url"],
            row["item_status"],
            row["error_message"] or "",
            d.get("research_status",""),
            d.get("hp_status",""),
            d.get("hp_rank",""),
            d.get("hp_score",""),
            cats,
            d.get("age_probability_under_59",""),
            d.get("marketing_signal_count",""),
            sigs,
            d.get("hotness",""),
            d.get("midday_procedure",""),
            d.get("midday_days",""),
            crawl,
            d.get("research_error",""),
            d.get("error",""),
        ])

print("作成しました:", OUT.resolve())
print("ジョブID:", job["id"])
print("開始:", job["created_at"])
print("状態:", job["status"])
print("件数:", len(rows))
