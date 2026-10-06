"""TASK 9 (phase4 overnight run): generate the v2-candidate Comdesk export by
reusing scripts/export_sales_list_to_comdesk.py verbatim (same UUID-integrity
guard, same duplicate-UUID/duplicate-clinic_id/no-Tier-D guards, same Comdesk
header mapping), only pointing its CLASSIFICATION_CSV at
sales_target_classification_final_v2_candidate.csv instead of the live final
CSV. No external upload (no such tool/capability exists in this run anyway).
Read-only on Production clinics.sqlite3 and the Treatment sidecar; writes only
under artifacts/comdesk_sales_export/final/v2_candidate/.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scripts.export_sales_list_to_comdesk as export_mod  # noqa: E402

V2_CANDIDATE_CSV = ROOT / "artifacts" / "sales_target_reclassification" / "sales_target_classification_final_v2_candidate.csv"
OUT_DIR = ROOT / "artifacts" / "comdesk_sales_export" / "final" / "v2_candidate"


def main() -> None:
    export_mod.CLASSIFICATION_CSV = V2_CANDIDATE_CSV

    args = argparse.Namespace(
        department=[], crestix_department=[], treatment_category=[],
        sales_tier=[], confidence=[], exclude_human_review=False,
        mode="clinic-level", out_dir=OUT_DIR,
    )
    summary = export_mod.run_export(args, out_dir=OUT_DIR, write=True)

    guard_checks = {
        "duplicate_uuid_is_zero": summary["duplicate_uuid"] == 0,
        "duplicate_clinic_id_is_zero": summary["duplicate_clinic_id"] == 0,
        "no_tier_d_exported": True,  # run_export itself raises GuardViolation otherwise; reaching here means PASS
        "comdesk_csv_uuid_set_matches_audit_csv_uuid_set": summary["comdesk_csv_uuid_set_matches_audit_csv_uuid_set"],
    }
    summary["task9_guard_checks"] = guard_checks
    summary["task9_overall"] = "PASS" if all(guard_checks.values()) else "FAIL"
    summary["source_classification_csv"] = str(V2_CANDIDATE_CSV)
    summary["external_upload_performed"] = False

    with (OUT_DIR / "task9_export_summary.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
