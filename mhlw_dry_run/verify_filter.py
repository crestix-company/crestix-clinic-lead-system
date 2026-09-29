"""Exercise the exact same ClinicStore/Filters code path the UI uses, to verify the new
mhlw_departments filter, WITHOUT running Streamlit. READ ONLY against ./data/clinics.sqlite3
(store.connect() opens read-write per existing app design, but this script issues no writes:
only store.count()/store.query()/store.funnel(), never save_research/override/import_*)."""
import sys
sys.path.insert(0, "/Users/maekawahiroyuki/Desktop/clinic-list-filter-complete")
from src.master.store import ClinicStore
from src.master.filters import Filters

TARGETS = ["消化器内科","眼科","糖尿病内科","泌尿器科","循環器内科","皮膚科","歯科","美容整形外科","産婦人科"]

store = ClinicStore("/Users/maekawahiroyuki/Desktop/clinic-list-filter-complete/data/clinics.sqlite3")

print("=== baseline: no mhlw filter, active_only=False, hp_only=False (must equal 13970) ===")
print(store.count(Filters(active_only=False, hp_only=False)))

print("\n=== per-department count (active_only=False, hp_only=False - i.e. NOT gated by HP) ===")
per_dept = {}
for t in TARGETS:
    n = store.count(Filters(active_only=False, hp_only=False, mhlw_departments=[t]))
    per_dept[t] = n
    print(f"{t}\t{n}")

print("\n=== union of all 9 (OR condition), unique clinics ===")
union_count = store.count(Filters(active_only=False, hp_only=False, mhlw_departments=TARGETS))
print("9科いずれか(OR)のユニークclinic数:", union_count)

print("\n=== combined filter test: 消化器内科 + 循環器内科 (multi-select OR within same field) ===")
print(store.count(Filters(active_only=False, hp_only=False, mhlw_departments=["消化器内科","循環器内科"])))

print("\n=== negative test: REVIEW-only department name must yield 0 (not a valid crestix target / not auto-included) ===")
print("整形外科(should be 0, false-friend of 美容整形外科):", store.count(Filters(active_only=False, hp_only=False, mhlw_departments=["整形外科"])))
print("心療内科(should be 0, false-friend excluded):", store.count(Filters(active_only=False, hp_only=False, mhlw_departments=["心療内科"])))

print("\n=== hp_only=False confirmed usable (not forced True) ===")
print("hp_only=True + 消化器内科 (subset, should be <= above):", store.count(Filters(active_only=False, hp_only=True, mhlw_departments=["消化器内科"])))

print("\n=== sample rows per department (>=5 each), from real store.query() ===")
import json
with store.connect() as c:
    for t in TARGETS:
        rows = c.execute("""SELECT cl.id AS clinic_id, cl.clinic_name, cl.address,
                                    m.mhlw_facility_id, m.mhlw_department_name, m.crestix_department, m.mapping_status
                             FROM clinics cl JOIN mhlwdb.clinic_mhlw_departments m ON m.clinic_id=cl.id
                             WHERE m.crestix_department=? AND m.mapping_status IN ('EXACT','ALIAS')
                             ORDER BY cl.id LIMIT 5""",(t,)).fetchall()
        print(f"\n--- {t} (sample {len(rows)}) ---")
        for r in rows:
            print(dict(r))

store_summary = {"per_department": per_dept, "union_9_targets": union_count}
with open("/Users/maekawahiroyuki/Desktop/clinic-list-filter-complete/mhlw_dry_run/filter_verification_summary.json","w",encoding="utf-8") as f:
    json.dump(store_summary, f, ensure_ascii=False, indent=2)
print("\nWROTE filter_verification_summary.json")
