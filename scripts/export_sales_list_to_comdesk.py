"""Sales-list -> Comdesk export pipeline (production CLI).

診療科目 + Crestix診療科 + 治療カテゴリ + Sales Tier + Confidence でフィルタ ->
最新Production UUID JOIN -> UUIDありのみ -> 重複0 -> Comdesk投入用CSV生成。

Read-only on Production clinics.sqlite3 and the Treatment sidecar. Never
re-crawls. Writes only under artifacts/comdesk_sales_export/. Never writes
to Production DB, Treatment sidecar, MHLW DB, or Comdesk source data.

Comdesk CSV schema is reused verbatim from the existing
src/master/comdesk.py::COMDESK_HEADERS - no column names are guessed.

Usage:
  python -m scripts.export_sales_list_to_comdesk --crestix-department 眼科 --treatment-category 白内障手術
  python -m scripts.export_sales_list_to_comdesk --sales-tier A --confidence HIGH
  python -m scripts.export_sales_list_to_comdesk --crestix-department 眼科 --preview
  python -m scripts.export_sales_list_to_comdesk --interactive
  python -m scripts.export_sales_list_to_comdesk --list-filter-options
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.master.comdesk import COMDESK_HEADERS  # noqa: E402

CLASSIFICATION_CSV = ROOT / "artifacts" / "sales_target_reclassification" / "sales_target_classification_final.csv"
CLINIC_DB = Path("/Users/maekawahiroyuki/CrestixData/clinic-lead/clinics.sqlite3")
SIDECAR = Path("/Users/maekawahiroyuki/CrestixData/clinic-lead/treatment_research_final.sqlite3")
MANIFEST = "phase7-fullrun-514bd9972b3bf20e"
FINAL_DIR = ROOT / "artifacts" / "comdesk_sales_export" / "final"

MULTI_VALUE_SEP = "|"
TIER_SHORTHAND = {"A": "VERIFIED_TREATMENT", "B": "LIKELY_TREATMENT", "C": "SPECIALTY_TARGET", "D": "UNKNOWN"}
TIER_PITCH_BASIS = {
    "VERIFIED_TREATMENT": "VERIFIED_TREATMENT",
    "LIKELY_TREATMENT": "LIKELY_TREATMENT",
    "SPECIALTY_TARGET": "SPECIALTY_ONLY",
}


class GuardViolation(Exception):
    pass


def ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    conn.row_factory = sqlite3.Row
    return conn


def load_classification() -> list[dict]:
    with CLASSIFICATION_CSV.open(encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f) if any(v.strip() for v in r.values())]


def load_clinic_master(clinic_ids: list[int]) -> dict[int, dict]:
    out = {}
    with ro(CLINIC_DB) as db:
        for chunk_start in range(0, len(clinic_ids), 400):
            chunk = clinic_ids[chunk_start:chunk_start + 400]
            ph = ",".join("?" * len(chunk))
            for r in db.execute(
                f"SELECT id, uuid, clinic_name, phone, address, prefecture, hp_url "
                f"FROM clinics WHERE id IN ({ph})", chunk,
            ):
                out[r["id"]] = dict(r)
    return out


def verify_uuid_integrity() -> None:
    """PHASE 7 hard guard: abort the whole run if Production itself has a UUID
    attached to more than one clinic_id. This is a Production data-integrity
    precondition, checked fresh (read-only) every run - never assumed."""
    with ro(CLINIC_DB) as db:
        bad = db.execute(
            "SELECT uuid, COUNT(*) c FROM clinics WHERE uuid<>'' GROUP BY uuid HAVING c>1"
        ).fetchall()
    if bad:
        raise GuardViolation(
            f"STOP: {len(bad)} UUID(s) in Production clinics.sqlite3 map to more than one "
            f"clinic_id: {[r[0] for r in bad][:5]}... Refusing to export until this Production "
            f"data integrity issue is resolved. No file written."
        )


def load_full_treatment_categories(clinic_ids: list[int]) -> dict[int, list[str]]:
    """All real treatment_category rows per clinic from the sidecar, restricted
    to statuses that back our A/B acceptance (CONFIRMED, or REVIEW with
    exclusion_context='NONE'). Tier C clinics have no treatment_category rows
    by design (department-only)."""
    out: dict[int, list[str]] = {}
    with ro(SIDECAR) as db:
        for chunk_start in range(0, len(clinic_ids), 400):
            chunk = clinic_ids[chunk_start:chunk_start + 400]
            ph = ",".join("?" * len(chunk))
            for r in db.execute(
                f"SELECT clinic_id, treatment_category_name, research_status, exclusion_context "
                f"FROM clinic_treatment_research_final WHERE clinic_id IN ({ph}) "
                f"AND (research_status='CONFIRMED' OR (research_status='REVIEW' AND exclusion_context='NONE'))",
                chunk,
            ):
                out.setdefault(r["clinic_id"], []).append(r["treatment_category_name"])
    return {cid: sorted(set(cats)) for cid, cats in out.items()}


def build_joined_rows() -> list[dict]:
    """PHASE 1: sales_target_classification_final.csv (9,097) JOIN current
    Production clinics.sqlite3 on clinic_id, using the live UUID column."""
    classification = load_classification()
    clinic_ids = [int(r["clinic_id"]) for r in classification]
    master = load_clinic_master(clinic_ids)
    full_categories = load_full_treatment_categories(clinic_ids)

    rows = []
    for r in classification:
        cid = int(r["clinic_id"])
        m = master.get(cid, {})
        tier = r["sales_tier"]
        rows.append({
            "clinic_id": cid,
            "uuid": m.get("uuid", "").strip(),
            "clinic_name": r["clinic_name"] or m.get("clinic_name", ""),
            "department": r["department"],
            "crestix_department": r["crestix_department"],
            "sales_tier": tier,
            "sales_category": r["sales_category"],
            "treatment_category": r["treatment_category"],
            "treatment_categories_full": full_categories.get(cid, []),
            "confidence": r["confidence"],
            "sales_usable": r["sales_usable"],
            "human_verified": r["human_verified"],
            "human_review_needed": r["human_review_needed"],
            "evidence_url": r["evidence_url"],
            "evidence_text": "",  # not available for this cohort (sidecar schema gap)
            "decision_reason": r["decision_reason"],
            "sales_pitch_basis": TIER_PITCH_BASIS.get(tier, ""),
            "phone": m.get("phone", ""),
            "address": m.get("address", ""),
            "prefecture": m.get("prefecture", ""),
            "hp_url": m.get("hp_url", ""),
        })
    if len({r["clinic_id"] for r in rows}) != len(rows):
        raise GuardViolation("STOP: duplicate clinic_id inside sales_target_classification_final.csv itself.")
    return rows


def phase1_report(rows: list[dict]) -> dict:
    by_tier = {}
    for tier, label in (("VERIFIED_TREATMENT", "A"), ("LIKELY_TREATMENT", "B"), ("SPECIALTY_TARGET", "C")):
        subset = [r for r in rows if r["sales_tier"] == tier]
        by_tier[label] = {
            "total": len(subset),
            "uuid_present": sum(1 for r in subset if r["uuid"]),
            "uuid_missing": sum(1 for r in subset if not r["uuid"]),
        }
    abc = [r for r in rows if r["sales_usable"] == "true"]
    d = [r for r in rows if r["sales_tier"] == "UNKNOWN"]
    return {
        "hp_cohort_total": len(rows),
        "A_plus_B_plus_C": len(abc),
        "D_unknown": len(d),
        "sales_usable_uuid_present": sum(1 for r in abc if r["uuid"]),
        "sales_usable_uuid_missing": sum(1 for r in abc if not r["uuid"]),
        "by_tier": by_tier,
    }


# ---- PHASE 2: filter options ----

def build_filter_options(rows: list[dict]) -> dict:
    options: dict[str, Counter] = {"department": Counter(), "crestix_department": Counter(),
                                    "treatment_category": Counter(), "sales_tier": Counter(),
                                    "confidence": Counter()}
    uuid_present_counts: dict[str, Counter] = {k: Counter() for k in options}
    abc = [r for r in rows if r["sales_usable"] == "true"]
    for r in abc:
        has_uuid = bool(r["uuid"])
        for d in r["department"].split(" / "):
            d = d.strip()
            if d:
                options["department"][d] += 1
                if has_uuid:
                    uuid_present_counts["department"][d] += 1
        for d in r["crestix_department"].split(" / "):
            d = d.strip()
            if d:
                options["crestix_department"][d] += 1
                if has_uuid:
                    uuid_present_counts["crestix_department"][d] += 1
        cats = r["treatment_categories_full"] or ([r["treatment_category"]] if r["treatment_category"] else [])
        for c in cats:
            options["treatment_category"][c] += 1
            if has_uuid:
                uuid_present_counts["treatment_category"][c] += 1
        options["sales_tier"][r["sales_tier"]] += 1
        if has_uuid:
            uuid_present_counts["sales_tier"][r["sales_tier"]] += 1
        options["confidence"][r["confidence"]] += 1
        if has_uuid:
            uuid_present_counts["confidence"][r["confidence"]] += 1

    result = {}
    for field, counter in options.items():
        result[field] = []
        for value, clinic_count in counter.most_common():
            present = uuid_present_counts[field][value]
            result[field].append({
                "value": value, "clinic_count": clinic_count,
                "uuid_present_count": present, "uuid_missing_count": clinic_count - present,
            })
    return result


def write_filter_options(rows: list[dict]) -> tuple[Path, Path]:
    FINAL_DIR.mkdir(parents=True, exist_ok=True)
    options = build_filter_options(rows)
    csv_path = FINAL_DIR / "filter_options.csv"
    json_path = FINAL_DIR / "filter_options.json"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["field", "value", "clinic_count", "uuid_present_count", "uuid_missing_count"])
        for field, entries in options.items():
            for e in entries:
                w.writerow([field, e["value"], e["clinic_count"], e["uuid_present_count"], e["uuid_missing_count"]])
    json_path.write_text(json.dumps(options, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return csv_path, json_path


# ---- filtering / guard / shaping ----

def normalize_tiers(values: list[str]) -> list[str]:
    return [TIER_SHORTHAND.get(v.upper(), v) for v in values]


def apply_filters(rows: list[dict], args: argparse.Namespace) -> list[dict]:
    out = rows
    if args.department:
        wanted = set(args.department)
        out = [r for r in out if any(d.strip() in wanted for d in r["department"].split(" / ") if d.strip())]
    if args.crestix_department:
        wanted = set(args.crestix_department)
        out = [r for r in out if any(d.strip() in wanted for d in r["crestix_department"].split(" / ") if d.strip())]
    if args.treatment_category:
        wanted = set(args.treatment_category)
        out = [r for r in out if (r["treatment_category"] in wanted)
               or any(c in wanted for c in r["treatment_categories_full"])]
    if args.sales_tier:
        wanted = set(normalize_tiers(args.sales_tier))
        out = [r for r in out if r["sales_tier"] in wanted]
    if args.confidence:
        wanted = set(args.confidence)
        out = [r for r in out if r["confidence"] in wanted]
    return out


def apply_sales_guard(rows: list[dict], exclude_human_review: bool) -> list[dict]:
    # sales_usable=true only (D UNKNOWN is sales_usable=false by construction).
    out = [r for r in rows if r["sales_usable"] == "true"]
    if exclude_human_review:
        out = [r for r in out if r["human_review_needed"] != "true"]
    return out


def to_clinic_level(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        cats = r["treatment_categories_full"] or ([r["treatment_category"]] if r["treatment_category"] else [])
        row = dict(r)
        row["treatment_categories_joined"] = MULTI_VALUE_SEP.join(cats)
        out.append(row)
    return out


def to_clinic_treatment_level(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        cats = r["treatment_categories_full"] or ([r["treatment_category"]] if r["treatment_category"] else [""])
        for cat in cats:
            row = dict(r)
            row["treatment_category_row"] = cat
            out.append(row)
    return out


def to_comdesk_row(r: dict) -> list[str]:
    row = [""] * len(COMDESK_HEADERS)
    mapping = {
        "UUID": r["uuid"], "名前": r["clinic_name"], "都道府県": r["prefecture"],
        "住所１": r["address"], "Tel1": r["phone"], "URL": r["hp_url"],
    }
    for field, value in mapping.items():
        row[COMDESK_HEADERS.index(field)] = value or ""
    return row


def preview_counts(guarded: list[dict], all_filtered_before_guard: list[dict]) -> dict:
    uuid_present = [r for r in guarded if r["uuid"]]
    by_tier = Counter(r["sales_tier"] for r in guarded)
    return {
        "matched_clinics": len(guarded),
        "uuid_present": len(uuid_present),
        "uuid_missing": len(guarded) - len(uuid_present),
        "A_B_C_breakdown": {"A": by_tier.get("VERIFIED_TREATMENT", 0),
                             "B": by_tier.get("LIKELY_TREATMENT", 0),
                             "C": by_tier.get("SPECIALTY_TARGET", 0)},
        "human_review_count": sum(1 for r in guarded if r["human_review_needed"] == "true"),
        "comdesk_export_would_write": len(uuid_present),
    }


def run_export(args: argparse.Namespace, out_dir: Path | None = None, write: bool = True) -> dict:
    verify_uuid_integrity()
    all_rows = build_joined_rows()
    input_sales_usable = sum(1 for r in all_rows if r["sales_usable"] == "true")

    filtered = apply_filters(all_rows, args)
    guarded = apply_sales_guard(filtered, args.exclude_human_review)
    filter_matched = len(guarded)

    uuid_present = [r for r in guarded if r["uuid"]]
    uuid_missing = filter_matched - len(uuid_present)

    if args.mode == "clinic-treatment-level":
        expanded = to_clinic_treatment_level(uuid_present)
    else:
        expanded = to_clinic_level(uuid_present)

    dup_uuid = [u for u, c in Counter(r["uuid"] for r in uuid_present).items() if c > 1]
    dup_clinic_id_clinic_level = []
    if args.mode != "clinic-treatment-level":
        dup_clinic_id_clinic_level = [cid for cid, c in Counter(r["clinic_id"] for r in expanded).items() if c > 1]
    if dup_uuid:
        raise GuardViolation(f"STOP: duplicate UUID in export set: {dup_uuid[:5]}")
    if dup_clinic_id_clinic_level:
        raise GuardViolation(f"STOP: duplicate clinic_id in clinic-level export: {dup_clinic_id_clinic_level[:5]}")
    if any(r["sales_tier"] == "UNKNOWN" for r in expanded):
        raise GuardViolation("STOP: a Tier-D (UNKNOWN) row reached the export set.")

    summary = {
        "input_sales_usable": input_sales_usable,
        "filter_matched_clinics": filter_matched,
        "uuid_present": len(uuid_present),
        "uuid_missing": uuid_missing,
        "exported": len(expanded),
        "duplicate_uuid": len(dup_uuid),
        "duplicate_clinic_id": len(dup_clinic_id_clinic_level),
        "filters": {
            "department": args.department, "crestix_department": args.crestix_department,
            "treatment_category": args.treatment_category,
            "sales_tier": normalize_tiers(args.sales_tier), "confidence": args.confidence,
            "exclude_human_review": args.exclude_human_review, "mode": args.mode,
        },
        "status": "SUCCESS",
    }

    if not write:
        return summary

    out_dir = out_dir or FINAL_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    comdesk_path = out_dir / f"comdesk_sales_final_{ts}.csv"
    audit_path = out_dir / f"comdesk_sales_final_{ts}_audit.csv"
    summary_path = out_dir / f"comdesk_sales_final_{ts}_summary.json"

    with comdesk_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(COMDESK_HEADERS)
        for r in expanded:
            w.writerow(to_comdesk_row(r))

    audit_cols = ["clinic_id", "uuid", "clinic_name", "department", "crestix_department",
                  "sales_tier", "sales_category", "treatment_categories", "confidence",
                  "human_review_needed", "evidence_text", "evidence_url", "decision_reason",
                  "sales_pitch_basis", "filter_conditions"]
    filter_str = json.dumps(summary["filters"], ensure_ascii=False)
    with audit_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=audit_cols)
        w.writeheader()
        for r in expanded:
            cats = (r.get("treatment_category_row") if "treatment_category_row" in r
                    else r.get("treatment_categories_joined", ""))
            w.writerow({
                "clinic_id": r["clinic_id"], "uuid": r["uuid"], "clinic_name": r["clinic_name"],
                "department": r["department"], "crestix_department": r["crestix_department"],
                "sales_tier": r["sales_tier"], "sales_category": r["sales_category"],
                "treatment_categories": cats, "confidence": r["confidence"],
                "human_review_needed": r["human_review_needed"], "evidence_text": r["evidence_text"],
                "evidence_url": r["evidence_url"], "decision_reason": r["decision_reason"],
                "sales_pitch_basis": r["sales_pitch_basis"], "filter_conditions": filter_str,
            })

    comdesk_uuid_set = {to_comdesk_row(r)[COMDESK_HEADERS.index("UUID")] for r in expanded}
    audit_uuid_set = {r["uuid"] for r in expanded}
    summary["comdesk_csv_uuid_set_matches_audit_csv_uuid_set"] = comdesk_uuid_set == audit_uuid_set
    summary["comdesk_csv_path"] = str(comdesk_path)
    summary["audit_csv_path"] = str(audit_path)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary["summary_path"] = str(summary_path)
    return summary


# ---- interactive mode ----

def _prompt_multiselect(label: str, options: list[str]) -> list[str]:
    print(f"\n{label}:")
    for i, opt in enumerate(options, 1):
        print(f"  [{i}] {opt}")
    print("  (番号をカンマ区切りで入力、空Enterで全件対象)")
    raw = input("> ").strip()
    if not raw:
        return []
    chosen = []
    for tok in raw.split(","):
        tok = tok.strip()
        if tok.isdigit() and 1 <= int(tok) <= len(options):
            chosen.append(options[int(tok) - 1])
    return chosen


def run_interactive() -> None:
    verify_uuid_integrity()
    all_rows = build_joined_rows()
    options = build_filter_options(all_rows)

    dept = _prompt_multiselect("1. 診療科 (department)", [e["value"] for e in options["department"]])
    crestix = _prompt_multiselect("2. Crestix診療科 (crestix_department)",
                                   [e["value"] for e in options["crestix_department"]])
    treat = _prompt_multiselect("3. 治療カテゴリ (treatment_category)",
                                 [e["value"] for e in options["treatment_category"]])
    tier_labels = {"VERIFIED_TREATMENT": "A VERIFIED_TREATMENT", "LIKELY_TREATMENT": "B LIKELY_TREATMENT",
                   "SPECIALTY_TARGET": "C SPECIALTY_TARGET"}
    tier_choice = _prompt_multiselect("4. Sales Tier", [tier_labels[e["value"]] for e in options["sales_tier"]
                                                          if e["value"] in tier_labels])
    tiers = [k for k, v in tier_labels.items() if v in tier_choice]
    confidence = _prompt_multiselect("5. Confidence", [e["value"] for e in options["confidence"]])

    print("\n6. Human Reviewが必要な医院を含めますか? [Y/n]")
    exclude_hr = input("> ").strip().lower() == "n"

    args = argparse.Namespace(department=dept, crestix_department=crestix, treatment_category=treat,
                               sales_tier=tiers, confidence=confidence, exclude_human_review=exclude_hr,
                               mode="clinic-level")
    preview_summary = run_export(args, write=False)
    print(f"\nMatched clinics: {preview_summary['filter_matched_clinics']}")
    print(f"UUID present: {preview_summary['uuid_present']}")
    print(f"UUID missing: {preview_summary['uuid_missing']}")
    print(f"Comdesk export: {preview_summary['uuid_present']}")

    print("\n7. Export? [y/N]")
    if input("> ").strip().lower() == "y":
        result = run_export(args, write=True)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("キャンセルしました。ファイルは作成していません。")


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--department", action="append", default=[])
    ap.add_argument("--crestix-department", action="append", default=[])
    ap.add_argument("--treatment-category", action="append", default=[])
    ap.add_argument("--sales-tier", action="append", default=[])
    ap.add_argument("--confidence", action="append", default=[], choices=["HIGH", "MEDIUM", "LOW"])
    ap.add_argument("--exclude-human-review", action="store_true")
    ap.add_argument("--mode", choices=["clinic-level", "clinic-treatment-level"], default="clinic-level")
    ap.add_argument("--preview", action="store_true", help="counts only, writes no files")
    ap.add_argument("--interactive", action="store_true", help="menu-driven filter selection")
    ap.add_argument("--list-filter-options", action="store_true",
                     help="write filter_options.csv/json and exit")
    ap.add_argument("--out-dir", type=Path, default=None, help="override output directory (for tests)")
    return ap.parse_args(argv)


def main() -> None:
    args = parse_args()

    if args.interactive:
        run_interactive()
        return

    if args.list_filter_options:
        rows = build_joined_rows()
        csv_path, json_path = write_filter_options(rows)
        print(json.dumps({"filter_options_csv": str(csv_path), "filter_options_json": str(json_path)},
                          ensure_ascii=False, indent=2))
        return

    if args.preview:
        verify_uuid_integrity()
        all_rows = build_joined_rows()
        filtered = apply_filters(all_rows, args)
        guarded = apply_sales_guard(filtered, args.exclude_human_review)
        print(json.dumps(preview_counts(guarded, filtered), ensure_ascii=False, indent=2))
        return

    summary = run_export(args, out_dir=args.out_dir)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
