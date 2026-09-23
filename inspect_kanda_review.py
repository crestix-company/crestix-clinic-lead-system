import sqlite3, json

con = sqlite3.connect(r"data\clinics.sqlite3")
con.row_factory = sqlite3.Row

row = con.execute("""
SELECT
    c.clinic_name,
    c.maps_website_url,
    r.result_json,
    r.rowid
FROM research_results r
JOIN clinics c ON c.id = r.clinic_id
WHERE c.clinic_name LIKE '%神田眼科診療所%'
ORDER BY r.rowid DESC
LIMIT 1
""").fetchone()

if row is None:
    print("NOT_FOUND")
    raise SystemExit(0)

data = json.loads(row["result_json"] or "{}")

print("clinic_name =", row["clinic_name"])
print("maps_url =", row["maps_website_url"])
print("hp_status =", data.get("hp_status"))
print("research_status =", data.get("research_status"))
print("hp_url =", data.get("hp_url"))
print("hp_match_reason =", data.get("hp_match_reason"))
print("crawl_errors =", data.get("crawl_errors"))
print("research_error =", data.get("research_error"))
print("error =", data.get("error"))
