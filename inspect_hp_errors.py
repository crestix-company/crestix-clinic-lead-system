import sqlite3
import json
from pathlib import Path

db_path = Path("data") / "clinics.sqlite3"

if not db_path.exists():
    print(f"DB_NOT_FOUND: {db_path.resolve()}")
    raise SystemExit(1)

con = sqlite3.connect(str(db_path))
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
ORDER BY r.rowid DESC
""").fetchall()

latest = {}
for row in rows:
    cid = row["clinic_id"]
    if cid not in latest:
        latest[cid] = row

errors = []

for row in latest.values():
    try:
        data = json.loads(row["result_json"] or "{}")
    except Exception as e:
        data = {"_json_error": repr(e)}

    if data.get("hp_status") == "ERROR":
        errors.append({
            "clinic_name": row["clinic_name"],
            "maps_url": row["maps_website_url"],
            "crawl_errors": data.get("crawl_errors"),
            "research_error": data.get("research_error"),
            "error": data.get("error"),
            "hp_match_reason": data.get("hp_match_reason"),
        })

print("ERROR_COUNT =", len(errors))

for i, item in enumerate(errors, 1):
    print()
    print(f"---- ERROR {i} ----")
    print("clinic_name   =", ascii(item["clinic_name"]))
    print("maps_url      =", item["maps_url"])
    print("crawl_errors  =", ascii(item["crawl_errors"]))
    print("research_error=", ascii(item["research_error"]))
    print("error         =", ascii(item["error"]))
    print("hp_match_reason=", ascii(item["hp_match_reason"]))
