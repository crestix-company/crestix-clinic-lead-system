"""Phase 5 (Crestix department aggregation) + Phase 6 (HP research candidates). READ ONLY dry run."""
import csv
import json
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, "/Users/maekawahiroyuki/Desktop/clinic-list-filter-complete")
csv.field_size_limit(sys.maxsize)

BASE = "/Users/maekawahiroyuki/Desktop/clinic-list-filter-complete/mhlw_dry_run"
DB_PATH = "/Users/maekawahiroyuki/Desktop/clinic-list-filter-complete/data/clinics.sqlite3"

CRESTIX_TARGETS = ["消化器内科", "眼科", "糖尿病内科", "泌尿器科", "循環器内科", "皮膚科", "歯科", "美容整形外科", "産婦人科"]

# ---- load Phase3 mapping: department_code -> (status, crestix_department) ----
code_to_target = {}
with open(f"{BASE}/mhlw_to_crestix_department_mapping.csv", encoding="utf-8", newline="") as f:
    for row in csv.DictReader(f):
        if row["status"] in ("EXACT", "ALIAS") and row["crestix_department"] in CRESTIX_TARGETS:
            code_to_target[row["department_code"]] = row["crestix_department"]

# ---- load Phase2 department master: mhlw_facility_id -> set of crestix targets it carries ----
facility_targets = defaultdict(set)
with open(f"{BASE}/mhlw_department_master.csv", encoding="utf-8", newline="") as f:
    for row in csv.DictReader(f):
        target = code_to_target.get(row["department_code"])
        if target:
            facility_targets[row["mhlw_facility_id"]].add(target)

# ---- load Phase4 join: only NAME_ADDRESS-confirmed rows carry a usable mhlw_facility_id ----
confirmed = []  # (clinic_id, mhlw_facility_id)
with open(f"{BASE}/legacy_mhlw_join.csv", encoding="utf-8", newline="") as f:
    for row in csv.DictReader(f):
        if row["confidence"] == "NAME_ADDRESS" and row["candidate_count"] == "1":
            fid = row["mhlw_facility_id_candidates"]
            confirmed.append((row["clinic_id"], fid))

print(f"confirmed NAME_ADDRESS join rows: {len(confirmed)}", file=sys.stderr)

# ---- Phase 5: per-department unique clinic count + pair count ----
dept_clinics = defaultdict(set)
dept_pairs = defaultdict(int)
clinic_targets = defaultdict(set)
for clinic_id, fid in confirmed:
    for target in facility_targets.get(fid, ()):
        dept_clinics[target].add(clinic_id)
        dept_pairs[target] += 1
        clinic_targets[clinic_id].add(target)

phase5_summary = {
    t: {"unique_clinic_count": len(dept_clinics.get(t, ())), "pair_count": dept_pairs.get(t, 0)}
    for t in CRESTIX_TARGETS
}
with open(f"{BASE}/phase5_department_counts.json", "w", encoding="utf-8") as out:
    json.dump(phase5_summary, out, ensure_ascii=False, indent=2)
print(json.dumps(phase5_summary, ensure_ascii=False, indent=2), file=sys.stderr)

# ---- Phase 6: HP research candidates ----
conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
conn.execute("PRAGMA query_only=ON")
conn.row_factory = sqlite3.Row
legacy = {r["id"]: r for r in conn.execute(
    "SELECT id, clinic_name, hp_url, hp_status, treatments_json, effective_json FROM clinics")}
conn.close()

# "HP Research未完了" は治療カテゴリ(treatments_json)の有無で判定する。
# hp_status/research_statusはHP本人確認(identity)のステータスであり、治療カテゴリ調査の完了とは別軸のため使わない。
candidates = []
research_status_counter = defaultdict(int)
for clinic_id_str, targets in clinic_targets.items():
    cid = int(clinic_id_str)
    row = legacy.get(cid)
    if row is None:
        continue
    hp_url = (row["hp_url"] or "").strip()
    if not hp_url:
        continue
    try:
        eff = json.loads(row["effective_json"]) if row["effective_json"] else {}
    except json.JSONDecodeError:
        eff = {}
    research_status = eff.get("research_status", "")
    research_status_counter[research_status or "(empty)"] += 1
    treatments_done = row["treatments_json"] not in ("[]", "", None)
    research_required = not treatments_done
    if not research_required:
        continue
    for target in sorted(targets):
        candidates.append({
            "clinic_id": cid, "clinic_name": row["clinic_name"], "official_hp_url": hp_url,
            "mhlw_department": target, "crestix_department": target,
            "existing_hp_status": row["hp_status"], "existing_research_status": research_status,
            "research_required": "TRUE",
        })

with open(f"{BASE}/hp_research_candidates.csv", "w", encoding="utf-8", newline="") as out:
    w = csv.DictWriter(out, fieldnames=["clinic_id", "clinic_name", "official_hp_url", "mhlw_department",
                                         "crestix_department", "existing_hp_status", "existing_research_status", "research_required"])
    w.writeheader()
    w.writerows(candidates)

print(f"HP research candidate rows (clinic x dept pairs): {len(candidates)}", file=sys.stderr)
print(f"HP research candidate unique clinics: {len({c['clinic_id'] for c in candidates})}", file=sys.stderr)
print(f"existing_research_status distribution among target+HP-url clinics: {dict(research_status_counter)}", file=sys.stderr)
