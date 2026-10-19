#!/usr/bin/env python3
"""Local, read-only default planner for the 328-clinic Gmail history import."""
from __future__ import annotations

import argparse
import csv
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.outreach.gmail_history_backfill import apply_verified_linkage_overrides, history_operation_id, plan_rows, planned_events, write_plan


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True, help="READ ONLY BACKFILL_PLAN.md used to validate source cohort")
    parser.add_argument("--verified-overrides", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--candidate-review", type=Path)
    parser.add_argument("--missing-draft-review", type=Path)
    parser.add_argument("--events-output", type=Path)
    parser.add_argument("--backfill-clinics-output", type=Path)
    parser.add_argument("--backfill-events-output", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="required; does not connect to any service")
    args = parser.parse_args()
    if not args.dry_run:
        parser.error("only --dry-run is implemented; online/apply backfill is intentionally unavailable")
    plan_text = args.plan.read_text(encoding="utf-8")
    if "328" not in plan_text or "Clinic ID" not in plan_text:
        parser.error("BACKFILL_PLAN.md does not describe the expected 328 Clinic ID cohort")
    with args.input.open(newline="", encoding="utf-8-sig") as stream:
        raw_source_rows = list(csv.DictReader(stream))
    overrides = []
    if args.verified_overrides:
        with args.verified_overrides.open(newline="", encoding="utf-8-sig") as stream:
            overrides = list(csv.DictReader(stream))
    counts = write_plan(args.input, args.output, overrides=overrides)
    source_rows = apply_verified_linkage_overrides(raw_source_rows, overrides)
    planned = plan_rows(source_rows)
    by_clinic = {str(r.clinic_id): r for r in planned}
    if args.backfill_clinics_output:
        args.backfill_clinics_output.parent.mkdir(parents=True, exist_ok=True)
        with args.backfill_clinics_output.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["clinic_id", "clinic_uuid", "clinic_name", "clinic_address", "clinic_phone", "recipient_email", "observed_state", "action"])
            for row in source_rows:
                if by_clinic[row["sales_sheet_clinic_id"]].disposition == "SKIP":
                    writer.writerow([row["sales_sheet_clinic_id"], row.get("clinic_uuid", ""), row.get("clinic_master_name", ""), row.get("clinic_master_address", ""), row.get("clinic_master_phone", ""), row.get("gmail_target_email", ""), row.get("current_gmail_state", ""), "BACKFILL_HISTORY_AND_BLOCK_DRAFTS"])
    if args.candidate_review:
        args.candidate_review.parent.mkdir(parents=True, exist_ok=True)
        with args.candidate_review.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["clinic_id", "clinic_name", "clinic_uuid", "clinic_address", "clinic_phone", "email", "candidate_clinic_ids", "candidate_address", "candidate_phone", "candidate_email", "verification_status", "resolution_evidence"])
            for row in raw_source_rows:
                if row.get("identity_review") == "HOLD_POSSIBLE_DUPLICATE_CLINIC_ID":
                    cid = row.get("sales_sheet_clinic_id")
                    resolved = by_clinic[cid].disposition == "SKIP" and cid in {o.get("clinic_id") for o in overrides}
                    evidence = next((o.get("evidence", "") for o in overrides if o.get("clinic_id") == cid), "") if resolved else ""
                    writer.writerow([cid, row.get("clinic_master_name"), row.get("clinic_uuid"), row.get("clinic_master_address"), row.get("clinic_master_phone"), row.get("gmail_target_email"), row.get("possible_duplicate_clinic_ids"), "", "", "", "RESOLVED_DIFFERENT_CLINIC_BY_PHONE_NAME" if resolved else "HOLD_RECHECK_ADDRESS_PHONE_EMAIL", evidence])
    if args.missing_draft_review:
        args.missing_draft_review.parent.mkdir(parents=True, exist_ok=True)
        with args.missing_draft_review.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["clinic_id", "clinic_name", "historical_draft_id", "currently_found", "observed_current_state", "gmail_evidence", "action"])
            for row in source_rows:
                if row.get("historical_sheet_draft_id") and row.get("historical_draft_id_currently_found", "").upper() not in {"YES", "TRUE", "FOUND"}:
                    writer.writerow([row.get("sales_sheet_clinic_id"), row.get("clinic_master_name"), row.get("historical_sheet_draft_id"), row.get("historical_draft_id_currently_found"), row.get("current_gmail_state"), row.get("gmail_state_search_result"), "HOLD_RECONCILE_ID_AS_SENT_SCHEDULED_TRASH_OR_EXPIRED"])
    event_count = 0
    if args.events_output:
        args.events_output.parent.mkdir(parents=True, exist_ok=True)
        with args.events_output.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["clinic_id", "status", "gmail_kind", "gmail_id", "message_id", "thread_id", "evidence"])
            for row in source_rows:
                for event in planned_events(row):
                    writer.writerow([event.clinic_id, event.status, event.gmail_kind, event.gmail_id, event.message_id, event.thread_id, event.evidence])
                    event_count += 1
    candidate_event_count = 0
    if args.backfill_events_output:
        args.backfill_events_output.parent.mkdir(parents=True, exist_ok=True)
        with args.backfill_events_output.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["clinic_id", "operation_id", "gmail_kind", "gmail_id", "message_id", "thread_id", "status", "evidence"])
            for row in source_rows:
                cid = row["sales_sheet_clinic_id"]
                if by_clinic[cid].disposition != "SKIP":
                    continue
                for event in planned_events(row):
                    operation_id = history_operation_id(event.clinic_id, event.gmail_kind, event.gmail_id)
                    writer.writerow([event.clinic_id, operation_id, event.gmail_kind, event.gmail_id, event.message_id, event.thread_id, event.status, event.evidence])
                    candidate_event_count += 1
    missing = sum(bool(r.get("historical_sheet_draft_id")) and r.get("historical_draft_id_currently_found", "").upper() not in {"YES", "TRUE", "FOUND"} for r in source_rows)
    print(f"rows=328 duplicate_gmail_ids={counts['duplicate_gmail_ids']} observed_events={event_count} backfill_clinics={counts['SKIP']} backfill_events={candidate_event_count} READY={counts['READY']} SKIP={counts['SKIP']} HOLD={counts['HOLD']} historical_draft_ids_unresolved={missing}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
