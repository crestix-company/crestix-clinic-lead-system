import sqlite3, json

con = sqlite3.connect(r"data\clinics.sqlite3")
con.row_factory = sqlite3.Row

row = con.execute("""
SELECT c.clinic_name, c.maps_website_url, r.result_json
FROM research_results r
JOIN clinics c ON c.id = r.clinic_id
WHERE c.clinic_name LIKE '%宮田眼科東京%'
ORDER BY r.rowid DESC
LIMIT 1
""").fetchone()

if row is None:
    print("NOT_FOUND")
    raise SystemExit(0)

data = json.loads(row["result_json"] or "{}")

print("clinic_name =", row["clinic_name"])
print("maps_url =", row["maps_website_url"])
print("research_status =", data.get("research_status"))
print("hp_status =", data.get("hp_status"))
print("treatment_categories =", data.get("treatment_categories"))
print("treatment_evidence =", data.get("treatment_evidence"))
print("age_probability_under_59 =", data.get("age_probability_under_59"))
print("marketing_signal_count =", data.get("marketing_signal_count"))
print("marketing_signals =", data.get("marketing_signals"))
print("midday_procedure =", data.get("midday_procedure"))
