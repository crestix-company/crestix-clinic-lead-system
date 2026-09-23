from pathlib import Path
import csv
import json
import sqlite3
from collections import Counter

ROOT = Path.cwd()
DB = ROOT / "data" / "clinics.sqlite3"

if not DB.exists():
    print("エラー：clinic-list-filter-complete フォルダで実行してください。")
    raise SystemExit(1)

con = sqlite3.connect(str(DB))
con.row_factory = sqlite3.Row

rows = con.execute("""
SELECT
    c.id AS clinic_id,
    c.clinic_name,
    c.prefecture,
    c.designation_date,
    c.maps_website_url,
    r.result_json
FROM clinics c
LEFT JOIN research_results r
  ON r.rowid = (
      SELECT rr.rowid
      FROM research_results rr
      WHERE rr.clinic_id = c.id
      ORDER BY rr.rowid DESC
      LIMIT 1
  )
WHERE c.merged_into IS NULL
  AND c.merge_hold=0
  AND c.active=1
  AND c.prefecture='東京都'
  AND c.maps_presence_status='MAPS_MATCHED_WEBSITE'
  AND c.maps_website_url<>''
ORDER BY c.id
""").fetchall()

def safe_json(s):
    try:
        return json.loads(s or "{}")
    except Exception:
        return {}

def join_list(v):
    if not v:
        return ""
    if isinstance(v, list):
        out = []
        for x in v:
            if isinstance(x, dict):
                out.append(x.get("name") or x.get("category") or x.get("label") or json.dumps(x, ensure_ascii=False))
            else:
                out.append(str(x))
        return " / ".join(x for x in out if x)
    return str(v)

output_rows = []

for row in rows:
    d = safe_json(row["result_json"])
    signals = d.get("marketing_signals") or []
    signal_names = []
    signal_evidence = []
    for s in signals:
        if isinstance(s, dict):
            name = s.get("name","")
            if name:
                signal_names.append(name)
            ev = s.get("evidence") or s.get("url") or ""
            if name or ev:
                signal_evidence.append(f"{name}｜{ev}")
        else:
            signal_names.append(str(s))

    age_prob = d.get("age_probability_under_59")
    age_prob_text = f"{age_prob*100:.1f}%" if isinstance(age_prob, (int,float)) else ""

    out = {
        "clinic_id": row["clinic_id"],
        "医院名": row["clinic_name"],
        "都道府県": row["prefecture"],
        "指定年月日": row["designation_date"],
        "Google Maps HP": row["maps_website_url"],
        "解析HP": d.get("hp_url",""),
        "research_status": d.get("research_status",""),
        "hp_status": d.get("hp_status",""),
        "HPランク": d.get("hp_rank",""),
        "HPスコア": d.get("hp_score",""),
        "治療カテゴリ": join_list(d.get("treatment_categories")),
        "院長名": d.get("doctor_name",""),
        "卒業年": d.get("graduation_year",""),
        "医籍登録年": d.get("license_registration_year",""),
        "59歳以下確率": age_prob_text,
        "年齢信頼度": d.get("age_estimation_confidence",""),
        "年齢根拠": d.get("age_estimation_source",""),
        "集客シグナル数": d.get("marketing_signal_count", len(signal_names)),
        "集客シグナル": " / ".join(signal_names),
        "集客根拠": " / ".join(signal_evidence),
        "アツさ": d.get("hot_status",""),
        "HP本人確認理由": join_list(d.get("hp_match_reason")),
        "crawl_errors": json.dumps(d.get("crawl_errors", []), ensure_ascii=False),
        "research_error": d.get("research_error",""),
        "error": d.get("error",""),
    }
    output_rows.append(out)

headers = list(output_rows[0].keys()) if output_rows else []

def write_csv(filename, subset):
    path = ROOT / filename
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        w.writeheader()
        w.writerows(subset)
    return path

all_path = write_csv("hp_research_results_v254_all_762.csv", output_rows)
success_path = write_csv("hp_research_results_v254_success.csv", [r for r in output_rows if r["research_status"] == "SUCCESS"])
review_path = write_csv("hp_research_results_v254_review.csv", [r for r in output_rows if r["research_status"] == "REVIEW"])
error_path = write_csv("hp_research_results_v254_error.csv", [r for r in output_rows if r["research_status"] == "ERROR"])
notfound_path = write_csv("hp_research_results_v254_not_found.csv", [r for r in output_rows if r["research_status"] == "NOT_FOUND"])

hot = []
for r in output_rows:
    try:
        sig_count = int(r["集客シグナル数"] or 0)
    except Exception:
        sig_count = 0
    if r["research_status"] == "SUCCESS" and sig_count >= 2:
        hot.append(r)

hot_path = write_csv("hp_research_results_v254_hot.csv", hot)

counts = Counter(r["research_status"] or "UNKNOWN" for r in output_rows)

print("=== v25.4 全762件 出力完了 ===")
print("対象:", len(output_rows), "件")
print("集計:", dict(counts))
print("集客シグナル2件以上:", len(hot), "件")
print("全件:", all_path.resolve())
print("SUCCESS:", success_path.resolve())
print("REVIEW:", review_path.resolve())
print("ERROR:", error_path.resolve())
print("NOT_FOUND:", notfound_path.resolve())
print("アツい候補:", hot_path.resolve())
print("DBは変更していません。")
