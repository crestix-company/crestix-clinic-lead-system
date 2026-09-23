import csv
import json
import sqlite3
from pathlib import Path

DB = Path("data") / "clinics.sqlite3"
OUT = Path("hp_research_results.csv")

def jload(value):
    try:
        return json.loads(value or "{}")
    except Exception:
        return {}

def join_names(items):
    return " / ".join(str(x) for x in items if x not in (None, "", []))

con = sqlite3.connect(str(DB))
con.row_factory = sqlite3.Row

rows = con.execute("""
SELECT
    c.id AS clinic_id,
    c.clinic_name,
    c.maps_website_url,
    r.result_json,
    r.rowid AS research_rowid
FROM research_results r
JOIN clinics c ON c.id = r.clinic_id
WHERE COALESCE(TRIM(c.maps_website_url), '') <> ''
ORDER BY r.rowid DESC
""").fetchall()

latest = {}
for row in rows:
    latest.setdefault(row["clinic_id"], row)

out_rows = []

for row in latest.values():
    d = jload(row["result_json"])
    hp_status = d.get("hp_status", "")
    research_status = d.get("research_status", "")
    if not hp_status and not research_status:
        continue

    signals = [x for x in d.get("marketing_signals", []) if isinstance(x, dict)]
    signal_names = [x.get("name", "") for x in signals]
    signal_details = [
        f'{x.get("name","")}｜{x.get("evidence_type","")}｜{x.get("evidence_url","")}'
        for x in signals
    ]

    treatment_evidence = []
    for x in d.get("treatment_evidence", []) or []:
        if not isinstance(x, dict):
            continue
        if float(x.get("confidence", 0) or 0) >= 0.7:
            treatment_evidence.append(
                f'{x.get("category","")}:{x.get("keyword","")}({x.get("confidence","")})｜{x.get("url","")}'
            )

    rank_reasons = [
        f'{x.get("feature","")}:+{x.get("points","")}'
        for x in d.get("hp_rank_reasons", []) or []
        if isinstance(x, dict)
    ]

    probability = d.get("age_probability_under_59")
    if isinstance(probability, (int, float)):
        probability_text = f"{probability*100:.1f}%"
    else:
        probability_text = ""

    out_rows.append({
        "clinic_id": row["clinic_id"],
        "医院名": row["clinic_name"],
        "Google Maps HP": row["maps_website_url"],
        "解析HP URL": d.get("hp_url", ""),
        "research_status": research_status,
        "hp_status": hp_status,
        "HPランク": d.get("hp_rank", ""),
        "HPスコア": d.get("hp_score", ""),
        "HPランク理由": join_names(rank_reasons),
        "治療カテゴリ": join_names(d.get("treatment_categories", []) or []),
        "治療根拠": join_names(treatment_evidence),
        "院長名": d.get("doctor_name", ""),
        "卒業年": d.get("graduation_year", ""),
        "59歳以下確率": probability_text,
        "年齢推定根拠": d.get("age_estimation_source", ""),
        "年齢推定信頼度": d.get("age_estimation_confidence", ""),
        "集客シグナル数": d.get("marketing_signal_count", len(signals)),
        "集客シグナル": join_names(signal_names),
        "集客シグナル根拠": join_names(signal_details),
        "アツさ": d.get("hot_status", ""),
        "昼の検査・手術専用枠": "あり" if "昼の検査・手術専用枠" in signal_names else "",
        "HP本人確認理由": join_names(d.get("hp_match_reason", []) or []),
        "クロールエラー数": len(d.get("crawl_errors", []) or []),
        "調査エラー": d.get("research_error", "") or d.get("error", ""),
    })

headers = [
    "clinic_id","医院名","Google Maps HP","解析HP URL","research_status","hp_status",
    "HPランク","HPスコア","HPランク理由","治療カテゴリ","治療根拠",
    "院長名","卒業年","59歳以下確率","年齢推定根拠","年齢推定信頼度",
    "集客シグナル数","集客シグナル","集客シグナル根拠","アツさ",
    "昼の検査・手術専用枠","HP本人確認理由","クロールエラー数","調査エラー"
]

with OUT.open("w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=headers)
    w.writeheader()
    w.writerows(sorted(out_rows, key=lambda x: (x["research_status"] != "SUCCESS", x["医院名"])))

success = sum(1 for x in out_rows if x["research_status"] == "SUCCESS")
review = sum(1 for x in out_rows if x["research_status"] == "REVIEW")
error = sum(1 for x in out_rows if x["research_status"] == "ERROR")

print(f"作成しました: {OUT.resolve()}")
print(f"全結果 {len(out_rows)}件 / 成功 {success}件 / 要確認 {review}件 / エラー {error}件")
print("このCSVをChatGPTにアップロードしてください。")
