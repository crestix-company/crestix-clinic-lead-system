"""Read-only audit of the 13,970-clinic legacy cohort versus Treatment Research.

No network functions are imported and all SQLite inputs are opened immutable.
Only CSV/JSON artifacts under --output-dir are written.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from scripts.phase7b_pilot import choose_identity_url

CUTOFF = "2026-09-28T02:26:53.465716+00:00"
EXPECTED_COHORT = 13970


def ro(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{path.expanduser().resolve()}?mode=ro&immutable=1", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def load_json(value, fallback):
    try:
        result = json.loads(value or "")
        return result if isinstance(result, type(fallback)) else fallback
    except (json.JSONDecodeError, TypeError):
        return fallback


def csv_write(path: Path, rows: list[dict], fields: list[str] | None = None) -> None:
    fields = fields or (list(rows[0]) if rows else [])
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clinic-db", required=True)
    parser.add_argument("--research-db", required=True)
    parser.add_argument("--mhlw-db", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    clinic_path, research_path, mhlw_path = map(Path, (args.clinic_db, args.research_db, args.mhlw_db))
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)

    with ro(clinic_path) as db:
        clinic_columns = [dict(cid=r[0], name=r[1], type=r[2], notnull=r[3], default=r[4], pk=r[5])
                          for r in db.execute("PRAGMA table_info(clinics)")]
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        table_schemas = {r[0]: r[1] for r in db.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"
        )}
        raw_rows = [dict(r) for r in db.execute("SELECT * FROM clinics WHERE first_seen_at<? ORDER BY id", (CUTOFF,))]
        if len(raw_rows) != EXPECTED_COHORT:
            raise ValueError(f"legacy cohort mismatch: {len(raw_rows)} != {EXPECTED_COHORT}")
        old_results = {int(r["clinic_id"]): load_json(r["result_json"], {}) for r in db.execute(
            "SELECT clinic_id,result_json FROM research_results"
        )}
        research_result_ids = set(old_results)
        json_keys = {}
        for column in ("base_json", "effective_json"):
            counts = Counter()
            for row in raw_rows:
                counts.update(load_json(row[column], {}).keys())
            json_keys[column] = dict(counts.most_common())
        result_keys = Counter()
        for value in old_results.values():
            result_keys.update(value.keys())
        json_keys["research_results.result_json"] = dict(result_keys.most_common())

    with ro(research_path) as db:
        new_rows: dict[int, list[dict]] = defaultdict(list)
        for row in db.execute(
            "SELECT clinic_id,treatment_category_name,research_status,matched_alias,source_url "
            "FROM clinic_treatment_research_final"
        ):
            new_rows[int(row["clinic_id"])].append(dict(row))
        new_status = {int(r["clinic_id"]): dict(r) for r in db.execute("SELECT * FROM clinic_research_status")}

    with ro(mhlw_path) as db:
        mhlw: dict[int, set[str]] = defaultdict(set)
        for clinic_id, department in db.execute(
            "SELECT clinic_id,mhlw_department_name FROM clinic_mhlw_departments_final WHERE mhlw_department_name<>''"
        ):
            mhlw[int(clinic_id)].add(department)

    records = []
    all_reason_counts = Counter()
    exclusive_counts = Counter()
    exclusive_order = [
        "MERGED", "MERGE_HOLD", "INACTIVE", "HOSPITAL_OR_CENTER", "NO_HP_URL",
        "NEW_RESEARCH_NOT_DONE", "IDENTITY_NOT_VERIFIED", "NO_NEW_TREATMENT_ROW",
        "NO_MHLW_DEPARTMENT", "UUID_ALREADY_EXISTS",
    ]
    for row in raw_rows:
        clinic_id = int(row["id"])
        old = old_results.get(clinic_id, {})
        old_treatments = load_json(row["treatments_json"], [])
        old_departments = load_json(row["departments_json"], [])
        effective = load_json(row["effective_json"], {})
        base = load_json(row["base_json"], {})
        url_record = dict(row)
        for payload in (effective, base):
            for key in ("hp_candidate_url", "hp_url"):
                if not url_record.get(key) and payload.get(key):
                    url_record[key] = payload[key]
        official_url, url_source = choose_identity_url(url_record, old)
        status = new_status.get(clinic_id, {})
        treatments = new_rows.get(clinic_id, [])
        facility_type = str(effective.get("facility_type") or base.get("facility_type") or "")
        hospital = (row["exclude_reason"] in ("hospital", "center") or facility_type == "病院"
                    or "病院" in row["clinic_name"] or "センター" in row["clinic_name"])
        old_researched = clinic_id in research_result_ids or row["hp_status"] != "UNRESEARCHED"
        identity_not_verified = (
            status.get("research_status") == "IDENTITY_NOT_VERIFIED"
            or status.get("last_error") == "IDENTITY_NOT_VERIFIED"
        )
        new_done = status.get("research_status") == "DONE"
        flags = {
            "NO_HP_URL": not bool(official_url),
            "OLD_HP_NOT_RESEARCHED": not old_researched,
            "NO_OLD_TREATMENT": not bool(old_treatments),
            "NEW_RESEARCH_NOT_DONE": not new_done,
            "NO_NEW_TREATMENT_ROW": not bool(treatments),
            "NO_MHLW_DEPARTMENT": clinic_id not in mhlw,
            "IDENTITY_NOT_VERIFIED": identity_not_verified,
            "UUID_ALREADY_EXISTS": bool(row["uuid"]),
            "INACTIVE": not bool(row["active"]),
            "MERGED": row["merged_into"] is not None,
            "MERGE_HOLD": bool(row["merge_hold"]),
            "HOSPITAL_OR_CENTER": hospital,
        }
        for reason, yes in flags.items():
            if yes:
                all_reason_counts[reason] += 1
        exclusive = next((reason for reason in exclusive_order if flags[reason]), "CURRENT_COMDESK_CANDIDATE")
        exclusive_counts[exclusive] += 1
        records.append({
            "clinic_id": clinic_id, "clinic_name": row["clinic_name"], "uuid": row["uuid"],
            "active": int(bool(row["active"])), "merged_into": row["merged_into"] or "",
            "merge_hold": int(bool(row["merge_hold"])), "hospital_or_center": int(hospital),
            "official_hp_url": official_url, "official_hp_url_source": url_source,
            "old_hp_status": row["hp_status"], "old_hp_researched": int(old_researched),
            "old_departments": " / ".join(old_departments),
            "old_treatments": " / ".join(old_treatments),
            "old_treatment_count": len(old_treatments),
            "new_research_status": status.get("research_status", "NOT_RESEARCHED"),
            "new_research_detail": status.get("last_error", ""),
            "new_treatment_row_count": len(treatments),
            "new_treatment_statuses": " / ".join(sorted({r["research_status"] for r in treatments})),
            "mhlw_departments": " / ".join(sorted(mhlw.get(clinic_id, set()))),
            "reason_flags": " / ".join(reason for reason, yes in flags.items() if yes),
            "exclusive_exclusion_reason": exclusive,
            "current_comdesk_candidate": int(exclusive == "CURRENT_COMDESK_CANDIDATE"),
            "_old_treatments": old_treatments, "_new_rows": treatments, "_old_result": old,
        })

    def count(predicate):
        return sum(bool(predicate(record)) for record in records)

    step_values = [
        ("旧対象母集団", len(records), "first_seen_at cutoff"),
        ("HP URLあり", count(lambda r: r["official_hp_url"]), "choose_identity_url priority"),
        ("旧HP調査済み", count(lambda r: r["old_hp_researched"]), "research_results or hp_status"),
        ("旧Treatmentカテゴリあり", count(lambda r: r["old_treatment_count"]), "clinics.treatments_json"),
        ("旧正式/正規化診療科あり", count(lambda r: r["old_departments"]), "clinics.departments_json"),
        ("新Treatment Research DONE", count(lambda r: r["new_research_status"] == "DONE"), "clinic_research_status"),
        ("新Treatment rowあり", count(lambda r: r["new_treatment_row_count"]), "post-guard sidecar"),
        ("MHLW正式診療科あり", count(lambda r: r["mhlw_departments"]), "MHLW final sidecar"),
        ("旧＋新Treatmentあり", count(lambda r: r["old_treatment_count"] and r["new_treatment_row_count"]), "clinic_id intersection"),
        ("旧Treatmentのみ", count(lambda r: r["old_treatment_count"] and not r["new_treatment_row_count"]), "legacy only"),
        ("新Treatmentのみ", count(lambda r: not r["old_treatment_count"] and r["new_treatment_row_count"]), "new only"),
        ("UUIDあり", count(lambda r: r["uuid"]), "clinics.uuid"),
        ("UUIDなし", count(lambda r: not r["uuid"]), "clinics.uuid"),
        ("現Comdesk候補", count(lambda r: r["current_comdesk_candidate"]), "exclusive eligibility after migration"),
    ]
    funnel_rows = []
    previous = None
    for step, value, basis in step_values:
        funnel_rows.append({"step": step, "clinic_count": value,
                            "change_from_previous": "" if previous is None else value - previous,
                            "definition_or_main_cause": basis})
        previous = value

    old_yes_new_yes = count(lambda r: r["old_treatment_count"] and r["new_treatment_row_count"])
    old_yes_new_no = count(lambda r: r["old_treatment_count"] and not r["new_treatment_row_count"])
    old_no_new_yes = count(lambda r: not r["old_treatment_count"] and r["new_treatment_row_count"])
    old_no_new_no = count(lambda r: not r["old_treatment_count"] and not r["new_treatment_row_count"])
    cross_rows = [
        {"row_type": "matrix", "old_treatment": "あり", "new_treatment": "あり", "category": "", "clinic_count": old_yes_new_yes},
        {"row_type": "matrix", "old_treatment": "あり", "new_treatment": "なし", "category": "", "clinic_count": old_yes_new_no},
        {"row_type": "matrix", "old_treatment": "なし", "new_treatment": "あり", "category": "", "clinic_count": old_no_new_yes},
        {"row_type": "matrix", "old_treatment": "なし", "new_treatment": "なし", "category": "", "clinic_count": old_no_new_no},
    ]
    old_only = [record for record in records if record["old_treatment_count"] and not record["new_treatment_row_count"]]
    old_only_categories = Counter(category for record in old_only for category in record["_old_treatments"])
    cross_rows += [
        {"row_type": "legacy_only_category", "old_treatment": "あり", "new_treatment": "なし",
         "category": category, "clinic_count": value}
        for category, value in old_only_categories.most_common()
    ]

    reason_rows = []
    # Add cumulative exclusive funnel positions and prove reconciliation. The two
    # legacy-only flags are descriptive overlaps, not gates in this funnel.
    running = len(records)
    for reason in exclusive_order:
        excluded = exclusive_counts.get(reason, 0)
        running -= excluded
        reason_rows.append({"reason": reason, "reason_flag_count": all_reason_counts.get(reason, 0),
                            "exclusive_first_exclusion_count": excluded,
                            "remaining_after_exclusive_step": running})
    for reason in ("OLD_HP_NOT_RESEARCHED", "NO_OLD_TREATMENT"):
        reason_rows.append({"reason": reason, "reason_flag_count": all_reason_counts.get(reason, 0),
                            "exclusive_first_exclusion_count": "",
                            "remaining_after_exclusive_step": ""})
    reason_rows.append({"reason": "CURRENT_COMDESK_CANDIDATE",
                        "reason_flag_count": exclusive_counts["CURRENT_COMDESK_CANDIDATE"],
                        "exclusive_first_exclusion_count": 0,
                        "remaining_after_exclusive_step": running})

    evidence_valid_sources = {"HOME_MENU", "INTRO_MENU", "DEDICATED_PAGE"}
    legacy_sales_treatment_categories = {"内視鏡", "白内障", "緑内障", "ICL", "ED", "男性更年期"}
    for record in old_only:
        evidence = record["_old_result"].get("treatment_evidence", [])
        high_evidence = [item for item in evidence if float(item.get("confidence", 0) or 0) >= .95]
        valid_sales_evidence = [
            item for item in high_evidence
            if item.get("category") in legacy_sales_treatment_categories
            and item.get("source") in evidence_valid_sources
        ]
        record["old_evidence_summary"] = " | ".join(
            f"{item.get('category','')}:{item.get('keyword','')}:{item.get('source','')}:{item.get('confidence','')}"
            for item in high_evidence[:8]
        )
        if record["new_research_status"] != "DONE":
            assessment = "5. Research対象・DB紐付けの問題"
            why = record["new_research_detail"] or record["new_research_status"]
        elif not record["official_hp_url"]:
            assessment = "5. Research対象・DB紐付けの問題"
            why = "有効な公式HP URLなし"
        elif valid_sales_evidence:
            if any(item.get("category") == "ED" for item in valid_sales_evidence):
                assessment = "4. Evidence条件が厳しくなったため新側にない"
            else:
                assessment = "3. 新taxonomyで細分化されたため対応しない"
            why = "旧HPメニューに営業Treatment evidenceがあるが新Treatment rowなし"
        elif high_evidence:
            assessment = "3. 新taxonomyで細分化されたため対応しない"
            why = "旧カテゴリは診療科・疾患中心で、新taxonomyの具体的Treatmentへ直接対応しない"
        else:
            assessment = "2. 旧Treatmentは旧ロジックの誤検出"
            why = "旧高confidence evidenceを確認できない"
        record["legacy_only_assessment"] = assessment
        record["new_treatment_absence_reason"] = why
        record["legacy_signal_eligible"] = int(bool(
            bool(valid_sales_evidence)
            and record["active"] and not record["merged_into"] and not record["merge_hold"]
            and not record["hospital_or_center"] and not record["uuid"] and record["mhlw_departments"]
            and "IDENTITY_NOT_VERIFIED" not in record["reason_flags"]
        ))

    # Category-stratified deterministic sample: one per category, then round-robin.
    sample = []
    selected = set()
    buckets = {category: [r for r in old_only if category in r["_old_treatments"]]
               for category in old_only_categories}
    while len(sample) < min(30, len(old_only)):
        added = False
        for category in sorted(buckets):
            candidate = next((r for r in buckets[category] if r["clinic_id"] not in selected), None)
            if candidate:
                sample.append(candidate)
                selected.add(candidate["clinic_id"])
                added = True
                if len(sample) == 30:
                    break
        if not added:
            break

    public_fields = [
        "clinic_id", "clinic_name", "old_departments", "old_treatments", "official_hp_url",
        "official_hp_url_source", "old_hp_status", "new_research_status", "new_research_detail",
        "new_treatment_row_count", "mhlw_departments", "uuid", "new_treatment_absence_reason",
        "legacy_only_assessment", "old_evidence_summary", "legacy_signal_eligible",
    ]
    new_signal = 2174  # post-guard audited NEW_CONFIRMED + NEW_REVIEW + NEW_MENTIONED
    legacy_signal_ids = {r["clinic_id"] for r in old_only if r.get("legacy_signal_eligible")}
    summary = {
        "scope": {"cutoff": CUTOFF, "clinic_count": len(records), "expected": EXPECTED_COHORT},
        "schema_audit": {
            "clinic_columns": clinic_columns, "tables": tables,
            "relevant_table_schemas": {name: sql for name, sql in table_schemas.items()
                                       if any(term in name.lower() for term in ("treatment", "research", "hp", "department", "category", "signal", "score"))},
            "json_keys": json_keys,
        },
        "legacy_treatment_samples": [
            {key: record[key] for key in (
                "clinic_id", "clinic_name", "old_treatments", "old_departments", "old_hp_status"
            )}
            for record in records if record["old_treatment_count"]
        ][:10],
        "definitions": {
            "legacy_treatment_ssot": "clinics.treatments_json",
            "legacy_treatment_origin": "research_results.result_json.treatment_categories projected through effective_json",
            "legacy_hp_researched": "research_results row exists OR clinics.hp_status != UNRESEARCHED",
            "effective_official_hp_url": "MAPS_MATCHED_WEBSITE maps URL, then saved hp_url/hp_candidate_url, then old research hp_url/candidates; official-candidate validation applied",
            "current_comdesk_candidate": "active, unmerged, no merge hold, not hospital/center, official HP, new DONE, identity verified, post-guard Treatment row, MHLW department, UUID empty",
        },
        "funnel": funnel_rows,
        "legacy_new_matrix": {
            "old_yes_new_yes": old_yes_new_yes, "old_yes_new_no": old_yes_new_no,
            "old_no_new_yes": old_no_new_yes, "old_no_new_no": old_no_new_no,
        },
        "reason_flag_counts": dict(all_reason_counts),
        "exclusive_funnel_counts": dict(exclusive_counts),
        "legacy_only_category_counts": dict(old_only_categories.most_common()),
        "legacy_only_assessment_counts": dict(Counter(r["legacy_only_assessment"] for r in old_only)),
        "legacy_signal": {
            "new_confirmed": 969,
            "new_review": 1169,
            "new_mentioned_not_confirmed": 36,
            "new_treatment_signal_only": new_signal,
            "legacy_treatment_signal_additional": len(legacy_signal_ids),
            "new_plus_legacy_unique": new_signal + len(legacy_signal_ids),
            "reaches_3000": new_signal + len(legacy_signal_ids) >= 3000,
        },
        "comdesk_scope_bridge": {
            "pre_guard_scope": 3716,
            "post_guard_no_treatment_candidate": 134,
            "post_migration_current_candidate": exclusive_counts["CURRENT_COMDESK_CANDIDATE"],
        },
        "input_sha256": {
            "clinics.sqlite3": sha256(clinic_path),
            "treatment_research_final.sqlite3": sha256(research_path),
            "mhlw_sidecar": sha256(mhlw_path),
        },
        "http_request_count": 0,
    }

    csv_write(output / "funnel_summary.csv", funnel_rows)
    csv_write(output / "legacy_new_cross.csv", cross_rows)
    csv_write(output / "exclusion_reasons.csv", reason_rows)
    csv_write(output / "legacy_only_clinics.csv", [{k: r.get(k, "") for k in public_fields} for r in old_only], public_fields)
    csv_write(output / "legacy_only_samples.csv", [{k: r.get(k, "") for k in public_fields} for r in sample], public_fields)
    (output / "funnel_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "funnel": funnel_rows, "matrix": summary["legacy_new_matrix"],
        "legacy_only_categories": summary["legacy_only_category_counts"],
        "legacy_only_assessments": summary["legacy_only_assessment_counts"],
        "exclusive": summary["exclusive_funnel_counts"], "legacy_signal": summary["legacy_signal"],
        "http_request_count": 0,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
