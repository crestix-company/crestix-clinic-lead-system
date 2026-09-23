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
    "clinic_id","医院名","Google Maps HP",
    "research_status","hp_status","HPランク","HPスコア","治療カテゴリ",
    "卒業年","医籍登録年","59歳以下確率",
    "集客シグナル数","集客シグナル","アツさ",
    "昼の検査・手術枠","昼枠曜日","昼枠根拠",
    "crawl_errors","research_error","error",
    "result_json"
]

def join_list(v):
    if isinstance(v, list):
        out = []
        for x in v:
            if isinstance(x, dict):
                out.append(x.get("name") or x.get("category") or json.dumps(x, ensure_ascii=False))
            else:
                out.append(str(x))
        return " / ".join(out)
    return "" if v is None else str(v)

with OUT.open("w", newline="", encoding="utf-8-sig") as f:
    w = csv.writer(f)
    w.writerow(headers)

    for row in rows:
        raw = row["result_json"] or "{}"
        try:
            d = json.loads(raw)
        except Exception:
            d = {}

        midday = d.get("midday_procedure")
        midday_slot = ""
        midday_day = ""
        midday_evidence = ""

        if isinstance(midday, dict):
            midday_slot = midday.get("procedure_slot", "")
            midday_day = midday.get("schedule_day", "")
            midday_evidence = midday.get("evidence", "")
        elif midday:
            midday_slot = str(midday)

        w.writerow([
            row["clinic_id"],
            row["clinic_name"],
            row["maps_website_url"],
            d.get("research_status",""),
            d.get("hp_status",""),
            d.get("hp_rank",""),
            d.get("hp_score",""),
            join_list(d.get("treatment_categories")),
            d.get("graduation_year",""),
            d.get("medical_license_year","") or d.get("license_year",""),
            d.get("age_probability_under_59",""),
            d.get("marketing_signal_count",""),
            join_list(d.get("marketing_signals")),
            d.get("hot_status","") or d.get("hotness",""),
            midday_slot,
            midday_day,
            midday_evidence,
            json.dumps(d.get("crawl_errors", []), ensure_ascii=False),
            d.get("research_error",""),
            d.get("error",""),
            raw,
        ])

print("作成しました:", OUT.resolve())
print("ジョブID:", job["id"])
print("開始:", job["created_at"])
print("状態:", job["status"])
print("件数:", len(rows))

# ざっくり集計
counts = {}
for row in rows:
    try:
        d = json.loads(row["result_json"] or "{}")
    except Exception:
        d = {}
    s = d.get("research_status") or d.get("hp_status") or "UNKNOWN"
    counts[s] = counts.get(s, 0) + 1

print("集計:", counts)
