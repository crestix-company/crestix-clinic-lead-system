import sqlite3, json

TARGETS = ["ストレスケア日比谷クリニック", "麹町クリニック"]

con = sqlite3.connect(r"data\clinics.sqlite3")
con.row_factory = sqlite3.Row

for name in TARGETS:
    row = con.execute("""
    SELECT c.clinic_name, c.maps_website_url, r.result_json
    FROM research_results r
    JOIN clinics c ON c.id = r.clinic_id
    WHERE c.clinic_name LIKE ?
    ORDER BY r.rowid DESC
    LIMIT 1
    """, (f"%{name}%",)).fetchone()

    print("=" * 70)
    if row is None:
        print(name, "NOT_FOUND")
        continue

    data = json.loads(row["result_json"] or "{}")

    print("clinic_name =", row["clinic_name"])
    print("maps_url =", row["maps_website_url"])
    print("research_status =", data.get("research_status"))
    print("hp_status =", data.get("hp_status"))
    print("treatment_categories =", data.get("treatment_categories"))
    print("clinic_name_treatment_categories =", data.get("clinic_name_treatment_categories"))
    print("treatment_evidence =", data.get("treatment_evidence"))

print("=" * 70)
