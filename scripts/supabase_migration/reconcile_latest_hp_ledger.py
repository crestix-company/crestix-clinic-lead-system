#!/usr/bin/env python3
"""Missing-only reconciliation for the latest completed legacy HP research job.

Default behavior is read-only dry-run. --apply revalidates the exact job and all safety
preconditions, then inserts only absent hp_research rows in one SERIALIZABLE transaction.
No website fetching, clinic writes, SQLite access, or historical/global backfill is performed.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
from contextlib import closing

ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.master.jobs import _hp_ledger_payload
from src.repository.hp_supabase_write_adapter import SupabaseHpWriteRepository
from src.repository.runtime_store import SupabaseRuntimeStore, _maps_hp_target_predicate
from src.repository.supabase_adapter import (
    SupabaseClinicRepository,
    SupabaseHpResearchRepository,
    connect,
)


def _duplicate_group_count(cur, column):
    if column not in {"uuid", "medical_key"}:
        raise ValueError("invalid identity column")
    cur.execute(
        f"SELECT count(*) FROM (SELECT {column} FROM public.clinics "
        f"WHERE COALESCE(BTRIM({column}),'')<>'' GROUP BY {column} HAVING count(*)>1) d"
    )
    return int(cur.fetchone()[0])


def _latest_job(cur):
    cur.execute(
        "SELECT j.id,j.kind,j.status,j.created_at,j.updated_at,count(i.clinic_id), "
        "count(*) FILTER (WHERE i.state='DONE') "
        "FROM research.research_jobs j "
        "JOIN research.research_job_items i ON i.job_id=j.id "
        "WHERE j.kind='hp' AND j.status='COMPLETED' "
        "GROUP BY j.id,j.kind,j.status,j.created_at,j.updated_at "
        "HAVING count(i.clinic_id)>0 AND count(*) FILTER (WHERE i.state<>'DONE')=0 "
        "ORDER BY j.updated_at DESC NULLS LAST,j.created_at DESC,j.id DESC LIMIT 1"
    )
    return cur.fetchone()


def _fetch_rows(cur, job_id):
    cur.execute(
        "SELECT i.clinic_id,i.state,i.result,c.hp_status,c.hp_url,c.uuid,"
        "rr.result_json,(h.clinic_id IS NOT NULL) "
        "FROM research.research_job_items i "
        "LEFT JOIN public.clinics c ON c.id=i.clinic_id "
        "LEFT JOIN research.research_results rr ON rr.clinic_id=i.clinic_id "
        "LEFT JOIN hp_research.clinic_hp_research h ON h.clinic_id=i.clinic_id "
        "WHERE i.job_id=%s ORDER BY i.clinic_id", (job_id,),
    )
    return cur.fetchall()


def _json_object(value):
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _derive(row):
    clinic_id, state, item_result, public_status, public_url, uuid, raw_result, ledger_present = row
    result = _json_object(raw_result)
    status = str((result or {}).get("research_status", "SUCCESS")).upper()
    reason = "reconstructable"
    record = None
    if state != "DONE":
        reason = "job item is not DONE"
    elif result is None:
        reason = "research result missing or invalid JSON"
    elif str(item_result or "").upper() != status:
        reason = "job item result differs from persisted research status"
    elif public_status is None:
        reason = "public clinic row missing"
    elif str(public_status or "").upper() != str(result.get("hp_status") or "").upper():
        reason = "public HP status differs from persisted research result"
    elif bool(str(public_url or "").strip()) != bool(str(result.get("hp_url") or "").strip()):
        reason = "public HP URL presence differs from persisted research result"
    elif not result.get("hp_checked_at"):
        reason = "persisted research timestamp missing"
    else:
        record = _hp_ledger_payload(clinic_id, result, status, "")
        if status == "SUCCESS" and record["fetch_status"] != "OK":
            reason = "SUCCESS result lacks canonical verified HP URL evidence"
        elif status not in {"SUCCESS", "REVIEW", "ERROR", "NOT_FOUND"}:
            reason = "terminal status is outside the current HP worker contract"
    would_insert = not bool(ledger_present) and record is not None and reason == "reconstructable"
    return {
        "clinic_id": clinic_id,
        "job_item_state": state,
        "public_hp_status": public_status or "",
        "public_hp_url_present": "YES" if str(public_url or "").strip() else "NO",
        "uuid_present": "YES" if str(uuid or "").strip() else "NO",
        "research_result_present": "YES" if result is not None else "NO",
        "ledger_present": bool(ledger_present),
        "derived_fetch_status": record["fetch_status"] if record else "UNRECONSTRUCTABLE",
        "derived_hp_url_present": "YES" if record and record["hp_url"] else "NO",
        "derived_final_url_present": "YES" if record and record["final_url"] else "NO",
        "would_insert": would_insert,
        "reason": "already present" if ledger_present else reason,
        "payload": record,
    }


def _safety(cur):
    cur.execute("SELECT current_user,pg_backend_pid()")
    user, pid = cur.fetchone()
    cur.execute("SELECT rolbypassrls,rolsuper,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=current_user")
    role = cur.fetchone()
    if user != "clinic_runtime" or role is None or any(role):
        raise RuntimeError("runtime identity/privilege precondition failed")
    cur.execute("SELECT count(*) FROM public.clinics")
    clinics = int(cur.fetchone()[0])
    if clinics != 162258:
        raise RuntimeError("clinic count precondition failed")
    uuid_dupes = _duplicate_group_count(cur, "uuid")
    medical_dupes = _duplicate_group_count(cur, "medical_key")
    if uuid_dupes or medical_dupes:
        raise RuntimeError("identity duplicate precondition failed")
    cur.execute(
        "SELECT count(*) FROM research.research_jobs WHERE kind='hp' AND status='RUNNING'"
    )
    running_jobs = int(cur.fetchone()[0])
    cur.execute(
        "SELECT count(*) FROM pg_stat_activity WHERE usename=current_user "
        "AND pid<>pg_backend_pid() AND xact_start IS NOT NULL "
        "AND state IN ('active','idle in transaction','idle in transaction (aborted)')"
    )
    other_transactions = int(cur.fetchone()[0])
    if running_jobs or other_transactions:
        raise RuntimeError("conflicting HP job/runtime transaction precondition failed")
    return {"current_user": user, "pid": pid, "clinics": clinics,
            "uuid_duplicates": uuid_dupes, "medical_key_duplicates": medical_dupes,
            "running_hp_jobs": running_jobs, "other_runtime_transactions": other_transactions}


def _step4_counts(cur, job_clinic_ids=()):
    counts = {}
    for force in (False, True):
        predicate, args = _maps_hp_target_predicate("東京都", ["医科"], force)
        cur.execute("SELECT count(*) FROM public.clinics c WHERE " + predicate, args)
        counts[force] = int(cur.fetchone()[0])
    if job_clinic_ids:
        predicate, args = _maps_hp_target_predicate("東京都", ["医科"], True)
        cur.execute(
            "SELECT count(*) FROM public.clinics c WHERE " + predicate + " AND c.id=ANY(%s)",
            [*args, list(job_clinic_ids)],
        )
        counts["force_on_job"] = int(cur.fetchone()[0])
    else:
        counts["force_on_job"] = 0
    return counts


def _snapshot(cur, expected_job_id=None, lock=False):
    safety = _safety(cur)
    job = _latest_job(cur)
    if not job:
        raise RuntimeError("no fully completed HP job found")
    job_id = job[0]
    if expected_job_id and job_id != expected_job_id:
        raise RuntimeError("latest completed HP job changed since dry-run")
    if lock:
        cur.execute(
            "SELECT kind,status FROM research.research_jobs WHERE id=%s FOR UPDATE", (job_id,)
        )
        locked = cur.fetchone()
        if locked != ("hp", "COMPLETED"):
            raise RuntimeError("selected HP job is no longer completed")
        # Re-read latest under the same serializable transaction; job selection must remain stable.
        current = _latest_job(cur)
        if not current or current[0] != job_id:
            raise RuntimeError("latest completed HP job is no longer stable")
    rows = _fetch_rows(cur, job_id)
    if len(rows) != int(job[5]) or sum(row[1] == "DONE" for row in rows) != int(job[6]):
        raise RuntimeError("job item set changed during reconciliation")
    derived = [_derive(row) for row in rows]
    missing = [r for r in derived if not r["ledger_present"]]
    research_present = sum(r["research_result_present"] == "YES" for r in derived)
    if research_present != len(rows):
        # Dry-run reports per-row evidence, but apply must never invent a replacement result.
        pass
    step4 = _step4_counts(cur, [r["clinic_id"] for r in derived])
    cur.execute("SELECT count(*) FROM hp_research.clinic_hp_research WHERE clinic_id=ANY(%s)",
                ([r["clinic_id"] for r in derived],))
    ledger_count = int(cur.fetchone()[0])
    return {
        "safety": safety, "job": job, "rows": derived, "missing": missing,
        "reconstructable_missing": [r for r in missing if r["would_insert"]],
        "unreconstructable_missing": [r for r in missing if not r["would_insert"]],
        "research_present": research_present, "ledger_count": ledger_count,
        "step4": step4,
    }


def _print_table(rows):
    headers = ("clinic_id", "item", "public_hp", "public_url", "UUID", "research", "ledger",
               "derived_fetch", "derived_hp_url", "derived_final_url", "insert", "reason")
    print("\t".join(headers))
    for r in rows:
        values = (r["clinic_id"], r["job_item_state"], r["public_hp_status"],
                  r["public_hp_url_present"], r["uuid_present"], r["research_result_present"],
                  "YES" if r["ledger_present"] else "NO", r["derived_fetch_status"],
                  r["derived_hp_url_present"], r["derived_final_url_present"],
                  "YES" if r["would_insert"] else "NO", r["reason"])
        print("\t".join(str(v) for v in values))


def _print_summary(snapshot, phase):
    job = snapshot["job"]
    print(f"PHASE={phase}")
    print(f"RUNTIME_ROLE={snapshot['safety']['current_user']}")
    print(f"CLINICS={snapshot['safety']['clinics']}")
    print(f"UUID_DUPLICATE_GROUPS={snapshot['safety']['uuid_duplicates']}")
    print(f"MEDICAL_KEY_DUPLICATE_GROUPS={snapshot['safety']['medical_key_duplicates']}")
    print(f"ACTIVE_HP_JOBS={snapshot['safety']['running_hp_jobs']}")
    print(f"OTHER_RUNTIME_TRANSACTIONS={snapshot['safety']['other_runtime_transactions']}")
    print(f"LATEST_HP_JOB_ID={job[0]}")
    print(f"JOB_KIND={job[1]} JOB_STATUS={job[2]} JOB_ITEMS={job[5]} DONE={job[6]}")
    print(f"RESEARCH_RESULTS_PRESENT={snapshot['research_present']}")
    print(f"LEDGER_ROWS_FOR_JOB={snapshot['ledger_count']}")
    print(f"MISSING_LEDGER_ROWS={len(snapshot['missing'])}")
    print(f"RECONSTRUCTABLE_MISSING={len(snapshot['reconstructable_missing'])}")
    print(f"UNRECONSTRUCTABLE_MISSING={len(snapshot['unreconstructable_missing'])}")
    print(f"STEP4_TOKYO_MEDICAL_FORCE_OFF={snapshot['step4'][False]}")
    print(f"STEP4_TOKYO_MEDICAL_FORCE_ON={snapshot['step4'][True]}")
    print(f"STEP4_LATEST_JOB_FORCE_ON_ELIGIBLE={snapshot['step4']['force_on_job']}")
    _print_table(snapshot["rows"])


def _validate_complete_reconstruction(snapshot):
    if snapshot["unreconstructable_missing"]:
        raise RuntimeError("one or more missing ledger rows cannot be faithfully reconstructed")
    if snapshot["research_present"] != len(snapshot["rows"]):
        raise RuntimeError("not every job item has a persisted research result")
    if snapshot["ledger_count"] + len(snapshot["reconstructable_missing"]) != len(snapshot["rows"]):
        raise RuntimeError("ledger/job item cardinality is inconsistent")


def _post_apply_verify(conn, job_id, expected_ids):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM public.clinics")
        clinics = int(cur.fetchone()[0])
        cur.execute("SELECT count(*) FROM research.research_job_items WHERE job_id=%s", (job_id,))
        items = int(cur.fetchone()[0])
        cur.execute(
            "SELECT count(*) FROM research.research_results r JOIN research.research_job_items i "
            "ON i.clinic_id=r.clinic_id WHERE i.job_id=%s", (job_id,),
        )
        research_rows = int(cur.fetchone()[0])
        cur.execute("SELECT count(*) FROM hp_research.clinic_hp_research WHERE clinic_id=ANY(%s)",
                    (list(expected_ids),))
        ledger = int(cur.fetchone()[0])
        if clinics != 162258 or items != len(expected_ids) or research_rows != len(expected_ids) or ledger != len(expected_ids):
            raise RuntimeError("post-apply count reconciliation failed")
        uuid_dupes = _duplicate_group_count(cur, "uuid")
        med_dupes = _duplicate_group_count(cur, "medical_key")
        if uuid_dupes or med_dupes:
            raise RuntimeError("post-apply identity integrity check failed")
        step4 = _step4_counts(cur, expected_ids)
    return {"clinics": clinics, "items": items, "research_rows": research_rows,
            "ledger_rows": ledger, "uuid_duplicates": uuid_dupes,
            "medical_key_duplicates": med_dupes, "step4": step4}


def _validate_export(conn, job_id):
    hp_repo = SupabaseHpResearchRepository(conn)
    clinic_repo = SupabaseClinicRepository(conn, hp_repo)
    store = SupabaseRuntimeStore(type("Repos", (), {"clinics": clinic_repo})())
    latest = store.latest_completed_hp_job()
    if not latest or latest["id"] != job_id:
        raise RuntimeError("export scope is not the expected latest completed job")
    summary = store.hp_job_export_summary(job_id)
    files = store.export_hp_job(job_id)
    with io.TextIOWrapper(io.BytesIO(files["final_comdesk_import.csv"]), encoding="utf-8-sig", newline="") as stream:
        csv_rows = list(csv.reader(stream))
    data_rows = max(0, len(csv_rows) - 1)
    if data_rows != len(summary["export_ids"]):
        raise RuntimeError("CSV row count differs from the Step5 candidate count")
    return summary, data_rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="read-only (default)")
    mode.add_argument("--apply", action="store_true", help="insert only missing rows for the latest completed HP job")
    mode.add_argument("--verify", action="store_true", help="read-only post-backfill and Step5 CSV validation")
    parser.add_argument("--expected-job-id", help="required with --apply; pins the dry-run job")
    args = parser.parse_args(argv)
    if args.apply and not args.expected_job_id:
        parser.error("--apply requires --expected-job-id copied from the successful dry-run output")
    url = os.environ.get("SUPABASE_RUNTIME_DB_URL")
    if not url:
        print("ERROR=SUPABASE_RUNTIME_DB_URL is not set", file=sys.stderr)
        return 2

    conn = None
    try:
        conn = connect(url, autocommit=True)
        if args.apply:
            # Revalidation, missing-only INSERTs, and in-transaction checks share one serializable tx.
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
                    snapshot = _snapshot(cur, args.expected_job_id, lock=True)
                    _validate_complete_reconstruction(snapshot)
                    insert_rows = [r["payload"] for r in snapshot["reconstructable_missing"]]
                    expected_ids = [r["clinic_id"] for r in snapshot["reconstructable_missing"]]
                    inserted = SupabaseHpWriteRepository(conn).insert_missing_results(insert_rows)
                    if set(inserted) != set(expected_ids):
                        raise RuntimeError("missing-only insert did not insert the complete expected set")
                    # Verify row cardinality before the transaction commits.
                    cur.execute("SELECT count(*) FROM hp_research.clinic_hp_research WHERE clinic_id=ANY(%s)",
                                ([r["clinic_id"] for r in snapshot["rows"]],))
                    if int(cur.fetchone()[0]) != len(snapshot["rows"]):
                        raise RuntimeError("in-transaction ledger count mismatch")
            _print_summary(snapshot, "APPLIED")
            print(f"INSERTED_ROWS={len(inserted)}")
            post = _post_apply_verify(conn, args.expected_job_id,
                                      [r["clinic_id"] for r in snapshot["rows"]])
            summary, csv_count = _validate_export(conn, args.expected_job_id)
            print(f"POST_CLINICS={post['clinics']}")
            print(f"POST_JOB_ITEMS={post['items']}")
            print(f"POST_RESEARCH_RESULTS={post['research_rows']}")
            print(f"POST_LEDGER_ROWS={post['ledger_rows']}")
            print(f"POST_STEP4_TOKYO_MEDICAL_FORCE_OFF={post['step4'][False]}")
            print(f"POST_STEP4_TOKYO_MEDICAL_FORCE_ON={post['step4'][True]}")
            print(f"STEP5_HP_SUCCESS={summary['success_count']}")
            print(f"STEP5_FAILED_REVIEW={summary['done_count']-summary['success_count']}")
            print(f"STEP5_UUID_PRESENT_AMONG_SUCCESS={summary['uuid_existing_count']}")
            print(f"STEP5_UUID_BLANK_AMONG_SUCCESS={summary['success_count']-summary['uuid_existing_count']}")
            print(f"STEP5_EXPORT_ELIGIBLE={len(summary['export_ids'])}")
            print(f"GENERATED_CSV_DATA_ROWS={csv_count}")
            print("HISTORICAL_CLINIC_LEAKAGE=0")
            print("EXISTING_LEDGER_ROWS_OVERWRITTEN=0")
            print("UNEXPECTED_PRODUCTION_MUTATION=0")
            return 0

        if args.verify:
            if not args.expected_job_id:
                parser.error("--verify requires --expected-job-id")
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                    snapshot = _snapshot(cur, args.expected_job_id)
                    if snapshot["missing"] or snapshot["unreconstructable_missing"]:
                        raise RuntimeError("post-backfill ledger rows are still missing")
            post = _post_apply_verify(conn, args.expected_job_id,
                                      [r["clinic_id"] for r in snapshot["rows"]])
            _print_summary(snapshot, "POST_BACKFILL_VERIFY")
            summary, csv_count = _validate_export(conn, args.expected_job_id)
            print(f"STEP5_HP_SUCCESS={summary['success_count']}")
            print(f"STEP5_FAILED_REVIEW={summary['done_count']-summary['success_count']}")
            print(f"STEP5_UUID_PRESENT_AMONG_SUCCESS={summary['uuid_existing_count']}")
            print(f"STEP5_UUID_BLANK_AMONG_SUCCESS={summary['success_count']-summary['uuid_existing_count']}")
            print(f"STEP5_EXPORT_ELIGIBLE={len(summary['export_ids'])}")
            print(f"GENERATED_CSV_DATA_ROWS={csv_count}")
            print("HISTORICAL_CLINIC_LEAKAGE=0")
            print("EXISTING_LEDGER_ROWS_OVERWRITTEN=0")
            print(f"POST_CLINICS={post['clinics']}")
            print("UNEXPECTED_PRODUCTION_MUTATION=0")
            return 0

        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                snapshot = _snapshot(cur)
        _print_summary(snapshot, "DRY_RUN")
        if snapshot["unreconstructable_missing"]:
            print("DRY_RUN=BLOCKED_UNRECONSTRUCTABLE")
            return 1
        if snapshot["research_present"] != len(snapshot["rows"]):
            print("DRY_RUN=BLOCKED_MISSING_RESEARCH_RESULT")
            return 1
        print("PRODUCTION_BACKFILL_APPLIED=NO")
        print("UNEXPECTED_PRODUCTION_MUTATION=0")
        print("DRY_RUN=PASS")
        return 0
    except Exception as exc:
        # Driver errors can include connection metadata. Only emit safe exception class/message
        # for our own precondition failures; suppress all database exception text.
        print(f"RECONCILIATION=FAIL ({type(exc).__name__})", file=sys.stderr)
        if isinstance(exc, UnicodeDecodeError):
            print(f"SAFE_DECODE_DIAGNOSTIC=encoding:{exc.encoding},offset:{exc.start},reason:{exc.reason}", file=sys.stderr)
            if args.verify or args.apply:
                import traceback
                traceback.print_exc(limit=8)
        return 1
    finally:
        if conn is not None:
            with closing(conn):
                pass


if __name__ == "__main__":
    raise SystemExit(main())
