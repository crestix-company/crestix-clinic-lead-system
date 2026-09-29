"""Phase 6.5: HP candidate URL re-audit (READ ONLY dry run).

Per explicit user instruction:
 - Do NOT loosen the NAME_ADDRESS 1:1 join criteria. Population = confirmed NAME_ADDRESS
   1:1 matches, restricted to the 9 Crestix target departments, with treatments_json
   not yet researched. REVIEW/UNMATCHED rows are excluded from this population.
 - Widen only the HP URL SOURCE: existing_hp_url, maps_website_url, mhlw_homepage_url
   (from MHLW 02-1/03-1 "案内用ホームページアドレス"), each kept in its own column.
 - Reuse the EXISTING production portal/aggregator blocklist (src.enrichment.hp_analysis
   .is_official_candidate / NON_OFFICIAL) instead of inventing a new one, so "portal site"
   judgement stays consistent with the running system.
 - Do NOT write anything back to clinics.sqlite3. Do NOT treat Maps/MHLW urls as a
   confirmed official HP - that judgement is left to Phase 7's actual fetch+identity check.
"""
import csv
import json
import sqlite3
import sys
from collections import defaultdict
from urllib.parse import urlparse

sys.path.insert(0, "/Users/maekawahiroyuki/Desktop/clinic-list-filter-complete")
csv.field_size_limit(sys.maxsize)

from src.enrichment.hp_analysis import is_official_candidate, host

BASE = "/Users/maekawahiroyuki/Desktop/clinic-list-filter-complete/mhlw_dry_run"
DB_PATH = "/Users/maekawahiroyuki/Desktop/clinic-list-filter-complete/data/clinics.sqlite3"
FACILITY_FILES = [
    ("医科", "/Users/maekawahiroyuki/Downloads/02-1_clinic_facility_info_20260601.csv"),
    ("歯科", "/Users/maekawahiroyuki/Downloads/03-1_dental_facility_info_20260601.csv"),
]
CRESTIX_TARGETS = ["消化器内科", "眼科", "糖尿病内科", "泌尿器科", "循環器内科", "皮膚科", "歯科", "美容整形外科", "産婦人科"]

# ---- 0. Confirm current DB state (before) ----
before_hash = sys.argv[1] if len(sys.argv) > 1 else None

# ---- 1. mhlw_facility_id -> 案内用ホームページアドレス ----
mhlw_hp = {}
for ftype, path in FACILITY_FILES:
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            url = (row["案内用ホームページアドレス"] or "").strip()
            if url:
                mhlw_hp[row["ID"]] = url

# ---- 2. Phase3 mapping + Phase2 master -> facility_id -> crestix targets (EXACT/ALIAS only) ----
code_to_target = {}
with open(f"{BASE}/mhlw_to_crestix_department_mapping.csv", encoding="utf-8", newline="") as f:
    for row in csv.DictReader(f):
        if row["status"] in ("EXACT", "ALIAS") and row["crestix_department"] in CRESTIX_TARGETS:
            code_to_target[row["department_code"]] = row["crestix_department"]

facility_targets = defaultdict(set)
with open(f"{BASE}/mhlw_department_master.csv", encoding="utf-8", newline="") as f:
    for row in csv.DictReader(f):
        t = code_to_target.get(row["department_code"])
        if t:
            facility_targets[row["mhlw_facility_id"]].add(t)

# ---- 3. Phase4 join -> ONLY confirmed NAME_ADDRESS 1:1 rows (join criteria untouched) ----
clinic_facility = {}  # clinic_id -> mhlw_facility_id
with open(f"{BASE}/legacy_mhlw_join.csv", encoding="utf-8", newline="") as f:
    for row in csv.DictReader(f):
        if row["confidence"] == "NAME_ADDRESS" and row["candidate_count"] == "1":
            clinic_facility[row["clinic_id"]] = row["mhlw_facility_id_candidates"]

# ---- 4. Read legacy clinics (READ ONLY) ----
conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
conn.execute("PRAGMA query_only=ON")
conn.row_factory = sqlite3.Row
legacy = {str(r["id"]): r for r in conn.execute(
    "SELECT id, clinic_name, address, hp_url, hp_status, maps_website_url, maps_presence_status, treatments_json FROM clinics")}
db_count_check = conn.execute("SELECT count(*) FROM clinics").fetchone()[0]
conn.close()

def norm_url(u):
    if not u:
        return ""
    p = urlparse(u.strip())
    if not p.scheme:
        p = urlparse("http://" + u.strip())
    h = (p.hostname or "").lower()
    if h.startswith("www."):
        h = h[4:]
    path = p.path.rstrip("/")
    return f"{h}{path}"

def official(u):
    return bool(u) and is_official_candidate(u)

# ---- 5. Population: NAME_ADDRESS confirmed + target dept + treatments not researched ----
pop = []
for cid, fid in clinic_facility.items():
    targets = facility_targets.get(fid)
    if not targets:
        continue
    row = legacy.get(cid)
    if row is None:
        continue
    if row["treatments_json"] not in ("[]", "", None):
        continue
    pop.append((cid, fid, row, sorted(targets)))

print(f"Phase6.5 population (NAME_ADDRESS + target dept + treatments未取得, clinic x確定dept合算前): {len(pop)}", file=sys.stderr)

# ---- 6. Per-clinic URL sourcing + aggregation ----
source_combo_counter = defaultdict(int)
url_relation_counter = defaultdict(int)
out_rows = []
for cid, fid, row, targets in pop:
    existing_raw = (row["hp_url"] or "").strip()
    maps_raw = (row["maps_website_url"] or "").strip() if row["maps_presence_status"] == "MAPS_MATCHED_WEBSITE" else ""
    mhlw_raw = mhlw_hp.get(fid, "")

    existing = existing_raw if official(existing_raw) else ""
    maps_url = maps_raw if official(maps_raw) else ""
    mhlw_url = mhlw_raw if official(mhlw_raw) else ""

    have = tuple(sorted(k for k, v in [("existing", existing), ("maps", maps_url), ("mhlw", mhlw_url)] if v))
    source_combo_counter[have or ("none",)] += 1

    norm_set = {k: norm_url(v) for k, v in [("existing", existing), ("maps", maps_url), ("mhlw", mhlw_url)] if v}
    domains = {k: host(v) for k, v in [("existing", existing), ("maps", maps_url), ("mhlw", mhlw_url)] if v}
    if len(norm_set) >= 2:
        vals = list(norm_set.values())
        if len(set(vals)) == 1:
            url_relation_counter["同一URL"] += 1
        elif len(set(domains.values())) == 1:
            url_relation_counter["同一ドメイン"] += 1
        else:
            url_relation_counter["別ドメイン(URL conflict)"] += 1

    selected_url, selected_source = "", ""
    if existing:
        selected_url, selected_source = existing, "existing_hp_url"
    elif maps_url:
        selected_url, selected_source = maps_url, "maps_website_url"
    elif mhlw_url:
        selected_url, selected_source = mhlw_url, "mhlw_homepage_url"

    for target in targets:
        out_rows.append({
            "clinic_id": cid, "mhlw_facility_id": fid, "clinic_name": row["clinic_name"], "address": row["address"],
            "crestix_department": target, "existing_hp_url": existing_raw, "maps_website_url": maps_raw,
            "mhlw_homepage_url": mhlw_raw, "selected_research_url": selected_url, "selected_url_source": selected_source,
            "treatments_status": "UNRESEARCHED" if row["treatments_json"] in ("[]", "", None) else "RESEARCHED",
        })

with open(f"{BASE}/hp_research_candidates_v2.csv", "w", encoding="utf-8", newline="") as out:
    fieldnames = ["clinic_id", "mhlw_facility_id", "clinic_name", "address", "crestix_department",
                  "existing_hp_url", "maps_website_url", "mhlw_homepage_url", "selected_research_url",
                  "selected_url_source", "treatments_status"]
    w = csv.DictWriter(out, fieldnames=fieldnames)
    w.writeheader()
    w.writerows(out_rows)

unique_clinics_any_url = len({r["clinic_id"] for r in out_rows if r["selected_research_url"]})
unique_clinics_total = len({r["clinic_id"] for r in out_rows})

report = {
    "population_unique_clinics": len(pop),
    "population_clinic_dept_pairs": len(out_rows),
    "unique_clinics_with_any_url": unique_clinics_any_url,
    "unique_clinics_no_url_at_all": unique_clinics_total - unique_clinics_any_url,
    "source_combo_breakdown_unique_clinics": {",".join(k): v for k, v in source_combo_counter.items()},
    "url_relation_breakdown": dict(url_relation_counter),
    "db_count_after": db_count_check,
}
with open(f"{BASE}/phase6_5_url_audit_summary.json", "w", encoding="utf-8") as out:
    json.dump(report, out, ensure_ascii=False, indent=2)
print(json.dumps(report, ensure_ascii=False, indent=2), file=sys.stderr)
