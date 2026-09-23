from pathlib import Path
import sqlite3
import traceback

from src.master.store import ClinicStore
from src.enrichment.researcher import Researcher

class NoSearch:
    def search(self, *args, **kwargs):
        raise RuntimeError("Unexpected search call: Maps HP should not require Tavily/search.")

db = Path("data") / "clinics.sqlite3"
store = ClinicStore(db)

with store.connect() as con:
    row = con.execute("""
        SELECT id, clinic_name, maps_website_url
        FROM clinics
        WHERE maps_website_url LIKE '%jckpf.or.jp%'
        ORDER BY id
        LIMIT 1
    """).fetchone()

if not row:
    print("TARGET_NOT_FOUND")
    raise SystemExit(1)

cid = row["id"]
record = store.get(cid)

print("TARGET")
print("clinic_id   =", cid)
print("clinic_name =", record.get("clinic_name"))
print("maps_url    =", record.get("maps_website_url"))
print()
print("診断を開始します。DBへの保存・変更は行いません。")
print()

try:
    researcher = Researcher(NoSearch(), max_pages=20)
    result, pages = researcher.hp(record, force=True)
    print("DIAG_RESULT = COMPLETED_WITHOUT_EXCEPTION")
    print("research_status =", result.get("research_status"))
    print("hp_status       =", result.get("hp_status"))
    print("hp_url          =", result.get("hp_url"))
    print("hp_match_reason =", result.get("hp_match_reason"))
    print("crawl_errors    =", result.get("crawl_errors"))
    print("pages           =", len(pages or []))
except Exception as exc:
    print("DIAG_RESULT = EXCEPTION")
    print("exception_type =", type(exc).__name__)
    print("exception      =", repr(exc))
    print()
    print("TRACEBACK")
    traceback.print_exc()
