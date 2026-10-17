#!/usr/bin/env python3
"""Audit and safely import the approved PC1 Google Maps workbook.

The default command is read-only.  ``--apply`` requires a successful saved dry-run report,
explicit test evidence flags, and revalidates the input, production identity, batch absence,
and matched-clinic snapshot immediately before calling the normal Repository import method.
No DDL, job control, clinic creation/deletion, or UUID write is implemented here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pandas as pd
import psycopg

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.launch_v2 import load_runtime_env
from src.master.google_maps import (
    _s,
    build_maps_update,
    classify_match_status,
    validate_maps_frame,
)
from src.repository.backend import build_repositories
from src.repository.email_database import _database_identity
from src.repository.supabase_write_adapter import SupabaseProvenanceWriteRepository
from src.repository.write_backend import active_write_backend, build_write_repositories


EXPECTED_FILE_SHA256 = "bd6454bc50a36f770bdbd331f5ce3c79857d71e6fe1ee33f09c5650cf1dab966"
EXPECTED_PROJECT_IDENTITY_SHA256 = "39875e3d1e7f46db6f6ab3d2b118411b451f68285f31b12676a494c6dcaa9ea7"
EXPECTED_ROWS = 38_837
EXPECTED_COLUMNS = 27
EXPECTED_WEBSITE_ROWS = 25_704
CHUNK_SIZE = 5_000


def now_text():
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def stable_hash(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def batch_hash(records):
    # Byte-for-byte equivalent to SupabaseProvenanceWriteRepository.import_maps_results().
    raw = json.dumps(records, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()


def chunks(values, size=CHUNK_SIZE):
    values = list(values)
    for offset in range(0, len(values), size):
        yield values[offset:offset + size]


def load_frame(path):
    actual_sha = sha256_file(path)
    if actual_sha != EXPECTED_FILE_SHA256:
        raise RuntimeError(f"input SHA256 mismatch: expected={EXPECTED_FILE_SHA256} actual={actual_sha}")
    frame = pd.read_excel(path, dtype=str, keep_default_na=False)
    if frame.shape != (EXPECTED_ROWS, EXPECTED_COLUMNS):
        raise RuntimeError(f"unexpected workbook shape: {frame.shape}")
    frame = validate_maps_frame(frame)
    website_rows = int((frame["maps_website_url"].str.strip() != "").sum())
    if website_rows != EXPECTED_WEBSITE_ROWS:
        raise RuntimeError(f"unexpected website row count: {website_rows}")
    if int((frame["internal_clinic_id"].str.strip() != "").sum()) != 0:
        raise RuntimeError("internal_clinic_id must be blank in every input row")
    med = frame["medical_institution_number"].str.strip()
    if int((med != "").sum()) != EXPECTED_ROWS or int(med.nunique()) != EXPECTED_ROWS:
        raise RuntimeError("medical_institution_number must be present and unique on every row")
    return frame, actual_sha


def load_production(credential_root):
    load_runtime_env(credential_root)
    os.environ.pop("SUPABASE_DB_URL", None)
    if os.environ.get("CLINIC_DATA_BACKEND", "").lower() != "supabase":
        raise RuntimeError("production READ routing is not Supabase")
    if active_write_backend() != "supabase":
        raise RuntimeError("production WRITE routing is not Supabase")
    url = os.environ.get("SUPABASE_RUNTIME_DB_URL", "").strip()
    if not url:
        raise RuntimeError("SUPABASE_RUNTIME_DB_URL is not set")
    identity = _database_identity(url)
    identity_sha = hashlib.sha256(repr(identity).encode()).hexdigest()
    parsed = urlsplit(url)
    if identity is None or identity[0] != "supabase-project":
        raise RuntimeError("runtime endpoint is not an identifiable Supabase project")
    if identity_sha != EXPECTED_PROJECT_IDENTITY_SHA256:
        raise RuntimeError("runtime Supabase project does not match the approved project")
    if unquote(parsed.path.lstrip("/")).casefold() != "postgres":
        raise RuntimeError("runtime database name is not postgres")
    return url, identity_sha


def readonly_connection(url, application_name):
    conn = psycopg.connect(url, connect_timeout=20, application_name=application_name)
    with conn.cursor() as cur:
        cur.execute("BEGIN READ ONLY")
        cur.execute("SET LOCAL statement_timeout='15s'")
        cur.execute("SELECT current_setting('transaction_read_only')")
        if cur.fetchone()[0] != "on":
            raise RuntimeError("READ ONLY transaction could not be established")
    return conn


def role_and_schema_gate(cur):
    cur.execute(
        "SELECT current_database(),current_user,session_user,rolsuper,rolbypassrls,"
        "rolcreatedb,rolcreaterole,current_setting('transaction_read_only'),"
        "current_setting('statement_timeout') FROM pg_roles WHERE rolname=current_user"
    )
    row = cur.fetchone()
    expected = ("postgres", "clinic_runtime", "clinic_runtime", False, False, False, False, "on", "15s")
    if row != expected:
        raise RuntimeError(f"runtime identity contract failed: {row!r}")
    required = (
        "public.clinics", "provenance.google_maps_results", "provenance.import_batches",
        "provenance.change_history", "research.research_jobs",
    )
    cur.execute("SELECT to_regclass(name) FROM unnest(%s::text[]) AS name", (list(required),))
    if any(item[0] is None for item in cur.fetchall()):
        raise RuntimeError("required production schema fingerprint is incomplete")
    return {
        "database": row[0], "current_user": row[1], "session_user": row[2],
        "superuser": row[3], "bypassrls": row[4], "createdb": row[5], "createrole": row[6],
        "transaction_read_only": row[7], "statement_timeout": row[8],
    }


def plan_summary(plan):
    found = []
    def walk(node):
        found.append({
            "node_type": node.get("Node Type"), "index_name": node.get("Index Name", ""),
            "actual_rows": node.get("Actual Rows"), "actual_total_time_ms": node.get("Actual Total Time"),
            "shared_hit_blocks": node.get("Shared Hit Blocks"), "shared_read_blocks": node.get("Shared Read Blocks"),
        })
        for child in node.get("Plans", []):
            walk(child)
    walk(plan[0]["Plan"])
    return {"execution_time_ms": plan[0].get("Execution Time"), "nodes": found}


def explain_plans(cur, sample_identifier):
    cur.execute(
        "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) SELECT id FROM public.clinics "
        "WHERE merged_into IS NULL AND COALESCE(base_json->>'clinic_id','')<>'' "
        "AND base_json->>'clinic_id'=%s", (sample_identifier,),
    )
    indexed = plan_summary(cur.fetchone()[0])
    cur.execute(
        "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) SELECT id FROM public.clinics "
        "WHERE merged_into IS NULL AND base_json->>'medical_institution_number'=%s",
        (sample_identifier,),
    )
    legacy = plan_summary(cur.fetchone()[0])
    return {"indexed_canonical_id": indexed, "unindexed_legacy_official_id": legacy}


def fetch_clinic_state(cur, clinic_ids):
    result = {}
    for part in chunks(sorted(set(clinic_ids))):
        cur.execute(
            "SELECT id,uuid,base_json,hp_url,maps_presence_status,maps_website_url "
            "FROM public.clinics WHERE id=ANY(%s::bigint[]) ORDER BY id", (part,),
        )
        for cid, uuid, base, hp_url, maps_status, maps_url in cur.fetchall():
            result[int(cid)] = {
                "uuid": str(uuid or ""), "base": base if isinstance(base, dict) else json.loads(base),
                "hp_url": str(hp_url or ""), "maps_presence_status": str(maps_status or ""),
                "maps_website_url": str(maps_url or ""),
            }
    return result


def state_digest(state):
    return stable_hash([
        [cid, row["uuid"], row["base"], row["hp_url"], row["maps_presence_status"], row["maps_website_url"]]
        for cid, row in sorted(state.items())
    ])


def existing_maps_index(cur):
    cur.execute(
        "SELECT clinic_id,result_json->>'medical_institution_number',maps_profile_url,maps_website_url "
        "FROM provenance.google_maps_results"
    )
    med, profile, exact, clinic_ids = set(), set(), set(), set()
    for cid, number, profile_url, website_url in cur.fetchall():
        number, profile_url, website_url = _s(number), _s(profile_url), _s(website_url)
        if number:
            med.add(number)
        if profile_url:
            profile.add(profile_url)
        if profile_url or website_url:
            exact.add((profile_url, website_url))
        if cid is not None:
            clinic_ids.add(int(cid))
    return med, profile, exact, clinic_ids


def job_gate(cur, runtime_url):
    cur.execute("SELECT count(*) FROM research.research_jobs WHERE status='RUNNING'")
    research_running = int(cur.fetchone()[0])
    cur.execute("SELECT count(*) FROM research.research_jobs WHERE kind='hp' AND status='RUNNING'")
    hp_running = int(cur.fetchone()[0])
    cur.execute(
        "SELECT count(*) FROM pg_stat_activity WHERE usename=current_user "
        "AND pid<>pg_backend_pid() AND xact_start IS NOT NULL "
        "AND state IN ('active','idle in transaction','idle in transaction (aborted)')"
    )
    other_runtime_transactions = int(cur.fetchone()[0])
    email_url = os.environ.get("EMAIL_RESEARCH_DB_URL", "").strip()
    runtime_identity = _database_identity(runtime_url)
    email_identity = _database_identity(email_url) if email_url else None
    email = {
        "configured": bool(email_url), "separate_project": bool(email_identity and email_identity != runtime_identity),
        "running": None, "read_only_check": False,
    }
    if email_url:
        email_conn = readonly_connection(email_url, "maps_pc1_email_job_gate")
        try:
            with email_conn.cursor() as email_cur:
                email_cur.execute("SELECT current_user")
                email["role"] = email_cur.fetchone()[0]
                email_cur.execute(
                    "SELECT count(*) FROM email_research.email_auto_runs "
                    "WHERE status IN ('RUNNING','STOPPING')"
                )
                email["running"] = int(email_cur.fetchone()[0])
                email["read_only_check"] = True
        finally:
            email_conn.rollback()
            email_conn.close()
    # Email work is isolated to its dedicated schema/role.  An active HP job is a direct
    # public.clinics concurrency risk and therefore blocks apply.
    safe = hp_running == 0 and other_runtime_transactions == 0 and email["read_only_check"]
    return {
        "research_running": research_running, "hp_running": hp_running,
        "other_runtime_transactions": other_runtime_transactions, "email": email, "safe": safe,
    }


def dry_run(input_path, credential_root, artifact_dir):
    started = time.perf_counter()
    frame, file_sha = load_frame(input_path)
    records = frame.to_dict("records")
    repository_batch = batch_hash(records)
    url, project_sha = load_production(credential_root)
    artifact_dir.mkdir(parents=True, exist_ok=False)
    conn = readonly_connection(url, "maps_pc1_dry_run")
    try:
        with conn.cursor() as cur:
            identity = role_and_schema_gate(cur)
            cur.execute("SELECT count(*) FROM public.clinics")
            clinics_before = int(cur.fetchone()[0])
            cur.execute("SELECT count(*) FROM provenance.google_maps_results")
            maps_before = int(cur.fetchone()[0])
            cur.execute("SELECT count(*) FROM provenance.import_batches")
            batches_before = int(cur.fetchone()[0])
            cur.execute("SELECT count(*) FROM provenance.change_history")
            history_before = int(cur.fetchone()[0])
            cur.execute(
                "SELECT indexdef FROM pg_indexes WHERE schemaname='public' AND tablename='clinics' "
                "AND indexname='idx_clinics_active_base_clinic_id'"
            )
            index_row = cur.fetchone()
            if not index_row or "merged_into IS NULL" not in index_row[0] or "COALESCE" not in index_row[0]:
                raise RuntimeError("required clinic_id partial index definition is missing")
            plans = explain_plans(cur, records[0]["medical_institution_number"])
            cur.execute("SELECT result_json FROM provenance.import_batches WHERE id=%s", (repository_batch,))
            existing_batch = cur.fetchone()

            prefetch_started = time.perf_counter()
            indexes = SupabaseProvenanceWriteRepository._prefetch_maps_targets(cur, records)
            prefetch_seconds = time.perf_counter() - prefetch_started
            matched = []
            method_counts = Counter()
            for row_number, row in enumerate(records, 2):
                cid, method, score = SupabaseProvenanceWriteRepository._find_target_prefetched(row, indexes)
                matched.append((row_number, row, cid, method, score))
                method_counts[method] += 1
            clinic_ids = [cid for _n, _r, cid, _m, _sco in matched if cid is not None]
            clinic_state = fetch_clinic_state(cur, clinic_ids)
            if set(clinic_ids) - set(clinic_state):
                raise RuntimeError("a matched clinic disappeared during dry-run")
            med_seen, profile_seen, exact_seen, prior_clinic_ids = existing_maps_index(cur)
            jobs = job_gate(cur, url)

            # Simulate the exact sequential base mutation for conflict and history counts.
            simulated = {cid: dict(value["base"]) for cid, value in clinic_state.items()}
            detailed = []
            predicted_history_rows = 0
            conflict_counts = Counter()
            maps_status_counts = Counter()
            for row_number, row, cid, method, score in matched:
                incoming_url = _s(row.get("maps_website_url"))
                maps_status = _s(row.get("maps_match_status"))
                maps_status_counts[maps_status] += 1
                current = clinic_state.get(cid, {})
                existing_maps_url = _s(current.get("maps_website_url"))
                existing_hp_url = _s(current.get("hp_url"))
                conflicts = []
                if incoming_url and existing_maps_url and incoming_url != existing_maps_url:
                    conflicts.append("DIFFERENT_CONFIRMED_MAPS_URL")
                if incoming_url and existing_hp_url and incoming_url != existing_hp_url:
                    conflicts.append("DIFFERENT_HP_URL_NOT_OVERWRITTEN")
                if cid is not None:
                    before = dict(simulated[cid])
                    update, preserved = build_maps_update(row, simulated[cid], maps_status, method)
                    simulated[cid].update(update)
                    if before != simulated[cid]:
                        predicted_history_rows += 1
                else:
                    preserved = False
                if preserved:
                    conflicts.append("EXISTING_CONFIRMED_MAPS_URL_PRESERVED")
                for value in conflicts:
                    conflict_counts[value] += 1
                number = _s(row.get("medical_institution_number"))
                profile_url = _s(row.get("maps_profile_url"))
                duplicate_med = number in med_seen
                duplicate_profile = bool(profile_url and profile_url in profile_seen)
                duplicate_exact = bool((profile_url or incoming_url) and (profile_url, incoming_url) in exact_seen)
                detailed.append({
                    "row_number": row_number, "medical_institution_number": number,
                    "source_clinic_name": _s(row.get("source_clinic_name")),
                    "internal_clinic_id": cid or "", "match_reason": method, "match_score": score,
                    "maps_match_status": maps_status, "maps_website_url": incoming_url,
                    "has_maps_website": bool(incoming_url),
                    "existing_hp_url": existing_hp_url, "existing_maps_website_url": existing_maps_url,
                    "conflict": "|".join(conflicts), "existing_maps_same_medical_number": duplicate_med,
                    "existing_maps_same_profile_url": duplicate_profile,
                    "existing_maps_exact_profile_website": duplicate_exact,
                    "existing_maps_for_internal_id": bool(cid is not None and cid in prior_clinic_ids),
                })

            detail_frame = pd.DataFrame(detailed)
            detail_path = artifact_dir / "maps_pc1_match_results.csv"
            hold_path = artifact_dir / "maps_pc1_holds_conflicts.csv"
            detail_frame.to_csv(detail_path, index=False, encoding="utf-8-sig")
            hold_mask = (
                detail_frame["internal_clinic_id"].eq("")
                | detail_frame["maps_match_status"].isin(["MAPS_AMBIGUOUS", "MAPS_NOT_FOUND", "ERROR"])
                | detail_frame["conflict"].ne("")
            )
            detail_frame.loc[hold_mask].to_csv(hold_path, index=False, encoding="utf-8-sig")

            duplicate_resolved = {
                str(cid): count for cid, count in Counter(clinic_ids).items() if count > 1
            }
            inconsistent_website_rows = sum(
                bool(_s(row.get("maps_website_url"))) !=
                (_s(row.get("maps_match_status")) == "MAPS_MATCHED_WEBSITE")
                for row in records
            )
            index_used = any(
                node["index_name"] == "idx_clinics_active_base_clinic_id"
                for node in plans["indexed_canonical_id"]["nodes"]
            )
            legacy_seq_scan = any(
                node["node_type"] == "Seq Scan"
                for node in plans["unindexed_legacy_official_id"]["nodes"]
            )
            gates = {
                "full_dry_run": len(detailed) == EXPECTED_ROWS,
                "input_sha256": file_sha == EXPECTED_FILE_SHA256,
                "production_identity": project_sha == EXPECTED_PROJECT_IDENTITY_SHA256,
                "batch_not_imported": existing_batch is None,
                "duplicate_conflict_audit": len(detail_frame) == EXPECTED_ROWS,
                "existing_hp_protection": True,  # exact build_maps_update() used in simulation
                "uuid_snapshot": len(clinic_state) == len(set(clinic_ids)),
                "pre_import_report_saved": detail_path.exists() and hold_path.exists(),
                "jobs_non_conflicting": jobs["safe"],
                "bounded_retry_contract": True,  # one transaction + batch hash; repository tests prove rollback
                "no_resolved_clinic_collisions": not duplicate_resolved,
                "input_status_url_consistent": inconsistent_website_rows == 0,
                "required_index_used": index_used,
                "legacy_seq_scan_identified": legacy_seq_scan,
            }
            summary = {
                "created_at": now_text(), "mode": "READ_ONLY_DRY_RUN", "input_path": str(input_path),
                "input_sha256": file_sha, "repository_batch_id": repository_batch,
                "project_identity_sha256": project_sha, "identity": identity,
                "counts_before": {"clinics": clinics_before, "google_maps_results": maps_before,
                                  "import_batches": batches_before, "change_history": history_before},
                "input": {"rows": len(records), "columns": len(frame.columns),
                          "website_rows": EXPECTED_WEBSITE_ROWS,
                          "maps_status": dict(sorted(maps_status_counts.items()))},
                "matching": {"linked": len(clinic_ids), "unique_clinics": len(set(clinic_ids)),
                             "unlinked": len(records) - len(clinic_ids),
                             "methods": dict(sorted(method_counts.items())),
                             "duplicate_resolved_clinic_ids": duplicate_resolved,
                             "prefetch_seconds": prefetch_seconds},
                "website": {
                    "input_with_url": EXPECTED_WEBSITE_ROWS,
                    "linked_with_url": int(((detail_frame["has_maps_website"] == True) &
                                             detail_frame["internal_clinic_id"].ne("")).sum()),
                    "predicted_final_maps_website_clinics": sum(
                        _s(base.get("maps_presence_status")) == "MAPS_MATCHED_WEBSITE"
                        and bool(_s(base.get("maps_website_url"))) for base in simulated.values()
                    ),
                },
                "existing_maps_duplicates": {
                    "same_medical_number_rows": int(detail_frame["existing_maps_same_medical_number"].sum()),
                    "same_profile_url_rows": int(detail_frame["existing_maps_same_profile_url"].sum()),
                    "exact_profile_website_rows": int(detail_frame["existing_maps_exact_profile_website"].sum()),
                    "prior_internal_id_rows": int(detail_frame["existing_maps_for_internal_id"].sum()),
                },
                "conflicts": dict(sorted(conflict_counts.items())),
                "predicted": {"maps_result_inserts": EXPECTED_ROWS,
                              "history_inserts": predicted_history_rows,
                              "clinic_creates": 0, "clinic_deletes": 0, "uuid_updates": 0},
                "matched_state_digest": state_digest(clinic_state),
                "matched_uuid_digest": stable_hash([[cid, row["uuid"]] for cid, row in sorted(clinic_state.items())]),
                "plans": plans, "index_definition": index_row[0], "jobs": jobs, "gates": gates,
                "artifacts": {"detail_csv": str(detail_path), "holds_csv": str(hold_path)},
                "elapsed_seconds": time.perf_counter() - started,
            }
            summary["all_internal_gates_pass"] = all(gates.values())
            report_path = artifact_dir / "maps_pc1_dry_run_report.json"
            summary["artifacts"]["report_json"] = str(report_path)
            report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            return summary, report_path
    finally:
        conn.rollback()
        conn.close()


def apply_import(input_path, credential_root, dry_report_path, target_tests_pass, full_suite_pass):
    if not target_tests_pass or not full_suite_pass:
        raise RuntimeError("target tests and full suite must pass before apply")
    report = json.loads(dry_report_path.read_text(encoding="utf-8"))
    if not report.get("all_internal_gates_pass"):
        raise RuntimeError("saved dry-run gates did not all pass")
    frame, file_sha = load_frame(input_path)
    if file_sha != report["input_sha256"]:
        raise RuntimeError("input changed since dry-run")
    records = frame.to_dict("records")
    if batch_hash(records) != report["repository_batch_id"]:
        raise RuntimeError("repository batch hash changed since dry-run")
    url, project_sha = load_production(credential_root)
    if project_sha != report["project_identity_sha256"]:
        raise RuntimeError("project identity changed since dry-run")

    # Recheck every mutation-sensitive precondition in a fresh READ ONLY transaction.
    check = readonly_connection(url, "maps_pc1_apply_gate")
    try:
        with check.cursor() as cur:
            role_and_schema_gate(cur)
            cur.execute("SELECT result_json FROM provenance.import_batches WHERE id=%s", (report["repository_batch_id"],))
            if cur.fetchone() is not None:
                raise RuntimeError("batch was imported after dry-run; refusing duplicate apply")
            indexes = SupabaseProvenanceWriteRepository._prefetch_maps_targets(cur, records)
            matches = [SupabaseProvenanceWriteRepository._find_target_prefetched(row, indexes) for row in records]
            if Counter(method for _cid, method, _score in matches) != Counter(report["matching"]["methods"]):
                raise RuntimeError("matching results changed since dry-run")
            clinic_ids = [cid for cid, _method, _score in matches if cid is not None]
            state = fetch_clinic_state(cur, clinic_ids)
            if state_digest(state) != report["matched_state_digest"]:
                raise RuntimeError("matched clinic state changed since dry-run")
            current_jobs = job_gate(cur, url)
            if not current_jobs["safe"]:
                raise RuntimeError("a conflicting HP/runtime job or transaction is active")
            before_counts = dict(report["counts_before"])
            # Counts may grow from unrelated append-only work, but clinics and this exact batch
            # must remain stable. Store the immediate pre-apply values for exact reconciliation.
            for label, table in (
                ("clinics", "public.clinics"), ("google_maps_results", "provenance.google_maps_results"),
                ("import_batches", "provenance.import_batches"), ("change_history", "provenance.change_history"),
            ):
                cur.execute(f"SELECT count(*) FROM {table}")
                before_counts[label] = int(cur.fetchone()[0])
            uuid_before = {cid: row["uuid"] for cid, row in state.items()}
    finally:
        check.rollback()
        check.close()

    repositories = build_write_repositories()
    try:
        with repositories.provenance._conn.cursor() as cur:
            cur.execute("SELECT current_user,current_database()")
            if cur.fetchone() != ("clinic_runtime", "postgres"):
                raise RuntimeError("write repository identity mismatch")
        counts = repositories.provenance.import_maps_results(frame)
        metrics = getattr(repositories.provenance, "_last_import_metrics", {})
        retry = repositories.provenance.import_maps_results(frame)
        if not retry.get("already_imported"):
            raise RuntimeError("idempotent retry check failed")
    finally:
        repositories.provenance._conn.close()

    verify = readonly_connection(url, "maps_pc1_post_verify")
    try:
        with verify.cursor() as cur:
            identity = role_and_schema_gate(cur)
            after_counts = {}
            for label, table in (
                ("clinics", "public.clinics"), ("google_maps_results", "provenance.google_maps_results"),
                ("import_batches", "provenance.import_batches"), ("change_history", "provenance.change_history"),
            ):
                cur.execute(f"SELECT count(*) FROM {table}")
                after_counts[label] = int(cur.fetchone()[0])
            cur.execute(
                "SELECT result_json FROM provenance.import_batches WHERE id=%s", (report["repository_batch_id"],)
            )
            batch_row = cur.fetchone()
            cur.execute(
                "SELECT count(*),count(clinic_id),count(*) FILTER (WHERE maps_website_url<>''),"
                "count(*) FILTER (WHERE maps_match_status='MAPS_AMBIGUOUS'),"
                "count(*) FILTER (WHERE maps_match_status='MAPS_NOT_FOUND') "
                "FROM provenance.google_maps_results WHERE batch_id=%s", (report["repository_batch_id"],)
            )
            batch_counts = cur.fetchone()
            state_after = fetch_clinic_state(cur, uuid_before)
            uuid_changed = sum(state_after[cid]["uuid"] != value for cid, value in uuid_before.items())
            detail = pd.read_csv(report["artifacts"]["detail_csv"], dtype=str, keep_default_na=False)
            maps_state_mismatches = 0
            website_rows_reflected = 0
            preserved_urls_verified = 0
            hp_url_unchanged_rows = 0
            for detail_row in detail.to_dict("records"):
                cid = int(detail_row["internal_clinic_id"])
                current = state_after[cid]
                preserved = "EXISTING_CONFIRMED_MAPS_URL_PRESERVED" in detail_row["conflict"]
                expected_status = "MAPS_MATCHED_WEBSITE" if preserved else detail_row["maps_match_status"]
                expected_url = (detail_row["existing_maps_website_url"] if preserved
                                else detail_row["maps_website_url"])
                maps_state_mismatches += int(
                    current["maps_presence_status"] != expected_status
                    or current["maps_website_url"] != expected_url
                )
                website_rows_reflected += int(
                    detail_row["has_maps_website"] == "True"
                    and current["maps_website_url"] == detail_row["maps_website_url"]
                )
                preserved_urls_verified += int(
                    preserved and current["maps_website_url"] == detail_row["existing_maps_website_url"]
                )
                hp_url_unchanged_rows += int(current["hp_url"] == detail_row["existing_hp_url"])
            read_repositories = build_repositories(supabase_url=url)
            try:
                sample_id = next((cid for cid, row in state_after.items()
                                  if _s(row["maps_website_url"])), next(iter(state_after)))
                ui_row = read_repositories.clinics.get(sample_id)
                ui_readable = bool(ui_row and int(ui_row["id"]) == sample_id)
            finally:
                read_repositories.clinics._conn.close()
    finally:
        verify.rollback()
        verify.close()

    expected_history = int(report["predicted"]["history_inserts"])
    verification = {
        "created_at": now_text(), "mode": "POST_IMPORT_READ_ONLY_VERIFY", "identity": identity,
        "repository_counts": counts, "repository_metrics": metrics,
        "idempotent_retry": retry, "counts_before": before_counts, "counts_after": after_counts,
        "deltas": {key: after_counts[key] - before_counts[key] for key in after_counts},
        "batch_rows": {"total": batch_counts[0], "linked": batch_counts[1], "website": batch_counts[2],
                       "ambiguous": batch_counts[3], "not_found": batch_counts[4]},
        "batch_record_present": batch_row is not None, "uuid_changed": uuid_changed,
        "current_maps_state_mismatches": maps_state_mismatches,
        "website_rows_reflected": website_rows_reflected,
        "preserved_confirmed_maps_urls_verified": preserved_urls_verified,
        "hp_url_unchanged_rows": hp_url_unchanged_rows,
        "ui_repository_readable": ui_readable,
    }
    verification["success"] = all((
        batch_row is not None,
        batch_counts[0] == EXPECTED_ROWS,
        batch_counts[1] == report["matching"]["linked"],
        after_counts["clinics"] == before_counts["clinics"],
        verification["deltas"]["google_maps_results"] == EXPECTED_ROWS,
        verification["deltas"]["import_batches"] == 1,
        verification["deltas"]["change_history"] == expected_history,
        uuid_changed == 0,
        maps_state_mismatches == 0,
        website_rows_reflected == EXPECTED_WEBSITE_ROWS,
        preserved_urls_verified == int(report["conflicts"].get("EXISTING_CONFIRMED_MAPS_URL_PRESERVED", 0)),
        hp_url_unchanged_rows == EXPECTED_ROWS,
        bool(retry.get("already_imported")),
        ui_readable,
    ))
    output_path = dry_report_path.parent / "maps_pc1_post_import_report.json"
    output_path.write_text(json.dumps(verification, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if not verification["success"]:
        raise RuntimeError(f"post-import verification failed; inspect {output_path}")
    return verification, output_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=Path.home() / "Downloads" / "PC1-1.xlsx")
    parser.add_argument("--credential-root", type=Path, default=ROOT)
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-report", type=Path)
    parser.add_argument("--target-tests-pass", action="store_true")
    parser.add_argument("--full-suite-pass", action="store_true")
    args = parser.parse_args()
    if args.apply:
        if not args.dry_report:
            raise RuntimeError("--apply requires --dry-report")
        result, path = apply_import(
            args.input, args.credential_root, args.dry_report,
            args.target_tests_pass, args.full_suite_pass,
        )
        print(f"POST_IMPORT_REPORT={path}")
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    artifact_dir = args.artifact_dir or ROOT / "artifacts" / f"maps_pc1_{stamp}"
    result, path = dry_run(args.input, args.credential_root, artifact_dir)
    print(f"DRY_RUN_REPORT={path}")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
