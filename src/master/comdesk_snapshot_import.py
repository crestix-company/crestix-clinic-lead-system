"""Opt-in orchestration for paired Comdesk sales and clinic CSV snapshots."""
from __future__ import annotations

import hashlib
import os

from src.master.comdesk_snapshot import (
    _same_identity,
    parse_comdesk_clinic_export,
    parse_comdesk_sales_export,
    plan_comdesk_snapshot_pair,
    snapshot_batch_key,
    snapshot_time_from_filenames,
)
from src.repository.comdesk_snapshot_repository import persist_snapshot_batch


def _ensure_development_import_enabled(connection) -> None:
    environment = (os.environ.get("CLINIC_ENVIRONMENT") or "").strip().casefold()
    enabled = (os.environ.get("CLINIC_COMDESK_SNAPSHOT_DIFF_ENABLED") or "").strip().casefold()
    expected_database = (os.environ.get("CLINIC_COMDESK_SNAPSHOT_DEVELOPMENT_DB_NAME") or "").strip()
    if environment != "development" or enabled not in {"1", "true", "yes", "on"} or not expected_database:
        raise RuntimeError("paired Comdesk snapshot import is disabled outside explicit development opt-in")
    with connection.cursor() as cursor:
        cursor.execute("select current_user, current_database()")
        current_user, current_database = cursor.fetchone()
    if current_user != "clinic_runtime":
        raise RuntimeError("paired snapshot import requires the least-privilege clinic_runtime login")
    if current_database != expected_database:
        raise RuntimeError("connected database does not match CLINIC_COMDESK_SNAPSHOT_DEVELOPMENT_DB_NAME")


def _plan_pair(connection, sales_bytes: bytes, clinic_bytes: bytes,
               sales_filename: str, clinic_filename: str, *, lock_snapshots=False):
    sales_rows = parse_comdesk_sales_export(sales_bytes, filename=sales_filename)
    clinic_rows = parse_comdesk_clinic_export(clinic_bytes, filename=clinic_filename)
    sales_digest = hashlib.sha256(sales_bytes).hexdigest()
    clinic_digest = hashlib.sha256(clinic_bytes).hexdigest()
    batch_key = snapshot_batch_key(sales_digest, clinic_digest)
    snapshot_at = snapshot_time_from_filenames(sales_filename, clinic_filename)
    uuids = sorted({str(row.get("uuid") or "").strip() for row in sales_rows + clinic_rows} - {""})
    names = sorted({str(row.get("clinic_name") or "").strip() for row in clinic_rows} - {""})
    addresses = sorted({str(row.get("address") or "").strip() for row in clinic_rows} - {""})
    phones = sorted({"".join(ch for ch in str(row.get("phone") or "") if ch.isdigit())
                     for row in clinic_rows} - {""})

    # Resolve already approved clinic/common IDs by UUID first. For a UUID not
    # linked yet, search existing clinics by exact name/address/normalized phone,
    # then require two independent matching identity fields in the planner.
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE clinic_comdesk_snapshot_runtime")
        cursor.execute(
            "SELECT DISTINCT c.id, c.clinic_name, c.address, c.phone, m.common_clinic_id, "
            "COALESCE(i.comdesk_uuid, '') "
            "FROM public.clinics c "
            "LEFT JOIN public.sales_entity_clinics m ON m.clinic_id = c.id "
            "LEFT JOIN public.sales_entity_comdesk_identity i ON i.common_clinic_id = m.common_clinic_id "
            "WHERE COALESCE(i.comdesk_uuid, '') = ANY(%s::text[]) "
            "OR c.clinic_name = ANY(%s::text[]) OR c.address = ANY(%s::text[]) "
            "OR regexp_replace(COALESCE(c.phone, ''), '[^0-9]', '', 'g') = ANY(%s::text[])",
            (uuids, names, addresses, phones),
        )
        master_rows = [{
            "clinic_id": row[0], "clinic_name": row[1], "address": row[2], "phone": row[3],
            "common_clinic_id": row[4], "canonical_comdesk_uuid": row[5],
        } for row in cursor.fetchall()]

    profiles_by_uuid = {}
    for row in clinic_rows:
        profiles_by_uuid.setdefault(str(row.get("uuid") or "").strip(), []).append(row)
    clinics_by_uuid = {}
    for uid in uuids:
        exact_uuid = [row for row in master_rows if row["canonical_comdesk_uuid"] == uid]
        if exact_uuid:
            clinics_by_uuid[uid] = exact_uuid
            continue
        profiles = profiles_by_uuid.get(uid, [])
        if len(profiles) != 1:
            clinics_by_uuid[uid] = []
            continue
        clinics_by_uuid[uid] = [row for row in master_rows if not row["canonical_comdesk_uuid"]
                                and _same_identity(profiles[0], row)[0]]

    common_ids = sorted({int(rows[0]["common_clinic_id"]) for rows in clinics_by_uuid.values()
                         if len(rows) == 1 and rows[0].get("common_clinic_id") is not None})
    existing = {}
    if common_ids:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT common_clinic_id, clinic_id, comdesk_uuid, progress, last_call_at, last_call_by, "
                "call_count, filter_project_name, project_keys, history_text, source_snapshot_at "
                "FROM public.sales_entity_comdesk_crm_snapshots WHERE common_clinic_id = ANY(%s::bigint[]) "
                "ORDER BY common_clinic_id" +
                (" FOR UPDATE" if lock_snapshots else ""),
                (common_ids,),
            )
            for row in cursor.fetchall():
                common_id, clinic_id, uid, progress, call_at, caller, calls, filter_project, projects, history, imported_at = row
                existing[int(common_id)] = {
                    "clinic_id": clinic_id, "comdesk_uuid": uid,
                    "sales_state": {"progress": progress, "last_call_at": call_at,
                                    "last_call_by": caller, "call_count": calls,
                                    "filter_project_name": filter_project},
                    "project_keys": tuple(projects or ()), "history_text": history or "",
                    "source_snapshot_at": imported_at.isoformat() if hasattr(imported_at, "isoformat") else str(imported_at),
                }

    plans = plan_comdesk_snapshot_pair(
        sales_rows, clinic_rows, clinics_by_uuid, existing,
        source_snapshot_at=snapshot_at, source_batch_key=batch_key,
    )
    return plans, sales_digest, clinic_digest, snapshot_at


def _summary(plans):
    result = {"insert": 0, "update": 0, "unchanged": 0, "hold": 0, "history_change": 0,
              "hold_reasons": {}}
    for plan in plans:
        if plan.action == "HOLD":
            result["hold"] += 1
            result["hold_reasons"][plan.reason] = result["hold_reasons"].get(plan.reason, 0) + 1
        else:
            result[plan.action.casefold()] += 1
            if plan.history_action in {"INITIAL", "CHANGE"}:
                result["history_change"] += 1
    return result


def preview_comdesk_snapshot_pair(connection, sales_bytes: bytes, clinic_bytes: bytes,
                                  sales_filename: str, clinic_filename: str):
    _ensure_development_import_enabled(connection)
    try:
        plans, _, _, _ = _plan_pair(connection, sales_bytes, clinic_bytes, sales_filename, clinic_filename)
        return _summary(plans)
    finally:
        # Preview performs SELECT only and must not leave a transaction open.
        connection.rollback()


def import_comdesk_snapshot_pair(connection, sales_bytes: bytes, clinic_bytes: bytes,
                                 sales_filename: str, clinic_filename: str):
    _ensure_development_import_enabled(connection)
    try:
        plans, sales_digest, clinic_digest, snapshot_at = _plan_pair(
            connection, sales_bytes, clinic_bytes, sales_filename, clinic_filename,
            lock_snapshots=True,
        )
        result = persist_snapshot_batch(
            connection, plans, sales_file_sha256=sales_digest,
            clinic_file_sha256=clinic_digest, source_snapshot_at=snapshot_at,
        )
        connection.commit()
        return {**_summary(plans), **result}
    except Exception:
        connection.rollback()
        raise
