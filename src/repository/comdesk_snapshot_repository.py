"""Transactional persistence for reviewed Comdesk CRM snapshots.

This repository only writes the additive snapshot/history tables. It never
creates clinic/common IDs, alters the UUID identity table, or emits call events.
"""
from __future__ import annotations

import json
from typing import Iterable, Mapping

from src.master.comdesk_snapshot import ComdeskSnapshotPlan


def persist_snapshot_batch(
    connection,
    plans: Iterable[ComdeskSnapshotPlan],
    *,
    sales_file_sha256: str,
    clinic_file_sha256: str,
    source_snapshot_at: str,
) -> dict[str, int | bool]:
    """Persist a batch exactly once. Connection must provide DB-API cursor().

    Caller is responsible for opening a transaction. UUID->clinic resolution
    and identity checks must already have been performed by the fail-closed
    planner against approved identity rows.
    """
    import hashlib
    from src.master.comdesk_snapshot import snapshot_batch_key

    if len(sales_file_sha256) != 64 or len(clinic_file_sha256) != 64:
        raise ValueError("both CSV SHA-256 digests are required")
    key = snapshot_batch_key(sales_file_sha256, clinic_file_sha256)
    plans = list(plans)
    if any(plan.source_batch_key != key for plan in plans):
        raise ValueError("plans must be generated for the same source file digest pair")
    if any(plan.source_snapshot_at != source_snapshot_at for plan in plans):
        raise ValueError("plans and persisted batch must use the same source snapshot timestamp")
    counts = {"inserted": 0, "updated": 0, "unchanged": 0, "held": 0, "history_changes": 0}
    cursor = connection.cursor()
    try:
        cursor.execute(
            "select sales_file_sha256, clinic_file_sha256 from public.sales_entity_comdesk_import_batches where source_batch_key = %s",
            (key,),
        )
        prior = cursor.fetchone()
        if prior:
            if tuple(prior) != (sales_file_sha256, clinic_file_sha256):
                raise RuntimeError("batch key collision: stored source digests do not match")
            return {"already_processed": True, **counts}

        cursor.execute(
            "insert into public.sales_entity_comdesk_import_batches "
            "(source_batch_key, sales_file_sha256, clinic_file_sha256, source_snapshot_at) values (%s, %s, %s, %s)",
            (key, sales_file_sha256, clinic_file_sha256, source_snapshot_at),
        )
        for plan in plans:
            if plan.action == "HOLD":
                fingerprint = hashlib.sha256(
                    f"{plan.comdesk_uuid}|{plan.reason}|{plan.history_hash}|{plan.sales_state}".encode("utf-8")
                ).hexdigest()
                cursor.execute(
                    "insert into public.sales_entity_comdesk_import_holds "
                    "(source_batch_key, source_uuid, reason, row_fingerprint) values (%s, %s, %s, %s) "
                    "on conflict do nothing",
                    (key, plan.comdesk_uuid, plan.reason, fingerprint),
                )
                counts["held"] += 1
                continue
            if plan.action == "UNCHANGED":
                cursor.execute(
                    "update public.sales_entity_comdesk_crm_snapshots set source_snapshot_at = %s, "
                    "source_batch_key = %s, updated_at = now() where common_clinic_id = %s "
                    "and clinic_id = %s and comdesk_uuid = %s returning common_clinic_id",
                    (plan.source_snapshot_at, key, plan.common_clinic_id, plan.clinic_id, plan.comdesk_uuid),
                )
                if cursor.fetchone() is None:
                    raise RuntimeError("snapshot identity changed since planning; refusing to update")
                counts["unchanged"] += 1
                continue

            # The UUID value originates in Comdesk. Link it only when no
            # identity exists yet; any pre-existing different pair aborts
            # the transaction rather than overwriting either identifier.
            cursor.execute(
                "select common_clinic_id, comdesk_uuid from public.sales_entity_comdesk_identity "
                "where common_clinic_id = %s or comdesk_uuid = %s for update",
                (plan.common_clinic_id, plan.comdesk_uuid),
            )
            identity_rows = cursor.fetchall()
            if identity_rows and (len(identity_rows) != 1 or tuple(identity_rows[0]) !=
                                  (plan.common_clinic_id, plan.comdesk_uuid)):
                raise RuntimeError("Comdesk UUID is already linked to a different common clinic; HOLD batch")
            if not identity_rows:
                cursor.execute(
                    "insert into public.sales_entity_comdesk_identity "
                    "(common_clinic_id, comdesk_uuid, source, evidence, verified_at) "
                    "values (%s, %s, %s, %s::jsonb, %s) returning common_clinic_id",
                    (plan.common_clinic_id, plan.comdesk_uuid, "comdesk_csv_identity_verified",
                     json.dumps({"verification": "unique clinic candidate; at least two normalized identity fields matched",
                                 "source_batch_key": key}), plan.source_snapshot_at),
                )
                if cursor.fetchone() is None:
                    raise RuntimeError("could not add Comdesk-provided UUID mapping")
                cursor.execute(
                    "insert into public.sales_entity_identity_audit "
                    "(event_key, common_clinic_id, action, before_state, after_state, reason, actor) "
                    "values (%s, %s, 'COMDESK_UUID_LINKED', '{}'::jsonb, %s::jsonb, %s, %s) "
                    "on conflict (event_key) do nothing",
                    ("comdesk-uuid-link:" + key + ":" + str(plan.common_clinic_id),
                     plan.common_clinic_id,
                     json.dumps({"comdesk_uuid": plan.comdesk_uuid}),
                     "Imported Comdesk UUID after unique normalized identity verification",
                     "comdesk_csv_import"),
                )

            cursor.execute(
                "insert into public.sales_entity_comdesk_crm_snapshots "
                "(common_clinic_id, clinic_id, comdesk_uuid, progress, last_call_at, last_call_by, call_count, "
                "project_keys, history_text, history_sha256, source_snapshot_at, source_batch_key) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "on conflict (common_clinic_id) do update set progress = excluded.progress, "
                "last_call_at = excluded.last_call_at, last_call_by = excluded.last_call_by, "
                "call_count = excluded.call_count, project_keys = excluded.project_keys, "
                "history_text = excluded.history_text, history_sha256 = excluded.history_sha256, "
                "source_snapshot_at = excluded.source_snapshot_at, source_batch_key = excluded.source_batch_key, "
                "updated_at = now() where sales_entity_comdesk_crm_snapshots.clinic_id = excluded.clinic_id "
                "and sales_entity_comdesk_crm_snapshots.comdesk_uuid = excluded.comdesk_uuid returning common_clinic_id",
                (plan.common_clinic_id, plan.clinic_id, plan.comdesk_uuid,
                 plan.sales_state.get("progress"), plan.sales_state.get("last_call_at"),
                 plan.sales_state.get("last_call_by"), plan.sales_state.get("call_count"),
                 list(plan.project_keys), plan.history_text, plan.history_hash,
                 plan.source_snapshot_at, key),
            )
            if cursor.fetchone() is None:
                raise RuntimeError("snapshot identity changed since planning; refusing to update")
            if plan.history_action in {"INITIAL", "CHANGE"}:
                previous = "" if plan.history_action == "INITIAL" else None
                previous_hash = hashlib.sha256(b"").hexdigest() if previous == "" else None
                if previous is None:
                    cursor.execute(
                        "select history_text, history_sha256 from public.sales_entity_comdesk_history_changes "
                        "where common_clinic_id = %s order by source_snapshot_at desc, id desc limit 1",
                        (plan.common_clinic_id,),
                    )
                    prior_history = cursor.fetchone()
                    previous, previous_hash = prior_history if prior_history else ("", hashlib.sha256(b"").hexdigest())
                cursor.execute(
                    "insert into public.sales_entity_comdesk_history_changes "
                    "(common_clinic_id, clinic_id, comdesk_uuid, source_batch_key, source_snapshot_at, "
                    "previous_history, previous_history_sha256, current_history, current_history_sha256) "
                    "values (%s, %s, %s, %s, %s, %s, %s, %s, %s) on conflict (common_clinic_id, source_batch_key) do nothing",
                    (plan.common_clinic_id, plan.clinic_id, plan.comdesk_uuid, key,
                     plan.source_snapshot_at, previous, previous_hash, plan.history_text, plan.history_hash),
                )
                counts["history_changes"] += 1
            counts["inserted" if plan.action == "INSERT" else "updated"] += 1
        return {"already_processed": False, **counts}
    finally:
        cursor.close()
