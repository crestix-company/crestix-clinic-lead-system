"""Read-only Comdesk CSV discovery and full-master reconciliation helpers."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import unicodedata
from collections import defaultdict
from pathlib import Path
from rapidfuzz import fuzz

from src.io.input_loader import load_table
from src.master.comdesk import infer_comdesk_columns, record_from_row, validate_comdesk_mapping
from src.normalizer.address import normalize_address
from src.normalizer.clinic_name import normalize_clinic_name
from src.normalizer.phone import tel_match_key


def header_key(value):
    return "".join(ch for ch in unicodedata.normalize("NFKC", str(value or "")).lstrip("\ufeff").casefold()
                   if not ch.isspace() and ch not in "_-")


def is_comdesk_auxiliary_export(headers):
    """Detect the sales/activity export, which must not enter clinic-master matching."""
    keys = {header_key(value) for value in headers}
    indicators = {"進捗", "最終架電日", "最終架電者", "コール数", "フィルタープロジェクト名", "プロジェクト"}
    return "uuid" in keys and len(keys & indicators) >= 2


def is_comdesk_export(headers):
    if len(headers) != 30 or is_comdesk_auxiliary_export(headers):
        return False
    keys = {header_key(value) for value in headers}
    expected = {"uuid", "種別", "名前", "tel1", "住所1", "住所１", "hpランク"}
    return len(keys & expected) >= 5


def discover_comdesk_csvs(directory):
    """Return unique-content Comdesk exports and exact-copy provenance metadata."""
    grouped = defaultdict(list)
    for path in sorted(Path(directory).rglob("*.csv")):
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        parsed = None
        for header_offset in range(4):
            try:
                table = load_table(raw, path.name, header_row=header_offset)
            except (ValueError, UnicodeError):
                continue
            if is_comdesk_export(table.headers):
                parsed = (table, header_offset)
                break
        if parsed is None:
            continue
        table, header_offset = parsed
        digest = hashlib.sha256(raw).hexdigest()
        grouped[digest].append((path, raw, table, header_offset))

    selected, copies = [], []
    for digest, members in grouped.items():
        members.sort(key=lambda member: (member[0].stat().st_mtime_ns, member[0].name), reverse=True)
        primary = members[0]
        table = primary[2]
        selected.append({
            "path": primary[0], "raw": primary[1], "table": table, "sha256": digest,
            "rows": len(table.data), "bytes": len(primary[1]),
            "local_mtime": primary[0].stat().st_mtime, "header_row_offset": primary[3],
        })
        for duplicate in members[1:]:
            copies.append({
                "duplicate_file": duplicate[0].name,
                "selected_file": primary[0].name,
                "sha256": digest,
                "rows": len(duplicate[2].data),
                "reason": "BYTE_IDENTICAL_FILE_COPY; omitted from matching input, source provenance retained",
            })
    return sorted(selected, key=lambda item: item["path"].name), copies


def source_scope(table, raw, path, mapping):
    aliases = {header_key(value) for value in (
        "フィルタープロジェクト名", "プロジェクト", "プロジェクト名", "プロジェクトID",
        "project", "project_name", "project_id"
    )}
    positions = [index for index, header in enumerate(table.headers) if header_key(header) in aliases]
    values = sorted({raw[index].strip() for index in positions if index < len(raw) and raw[index].strip()})
    if values:
        return ";".join(values), "CSV_PROJECT_COLUMN"
    membership_col = mapping.get("project_membership")
    if membership_col is not None and membership_col < len(raw) and raw[membership_col].strip():
        return raw[membership_col].strip(), "CSV_PROJECT_MEMBERSHIP"
    return path.stem, "FILENAME_ONLY_UNVERIFIED"


def input_rows(sources):
    items = []
    for source in sources:
        table = source["table"]
        mapping = infer_comdesk_columns(table)
        mapping.setdefault("uuid", None)
        mapping.setdefault("comdesk_case_id", None)
        validate_comdesk_mapping(table.headers, mapping)
        for zero_index, values in enumerate(table.data.values.tolist()):
            record = record_from_row(values, mapping)
            scope, scope_source = source_scope(table, values, source["path"], mapping)
            fingerprint = hashlib.sha256(
                json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            item = {
                "source_file": source["path"].name,
                "source_hash": source["sha256"],
                "source_row": zero_index + source["header_row_offset"] + 2,
                "project_scope": scope,
                "project_scope_source": scope_source,
                "header_row_offset": source["header_row_offset"],
                "record": record,
                "row_fingerprint": fingerprint,
                "_source_identity": input_identity(record),
            }
            items.append(item)
    return items


def input_identity(record):
    return (
        tel_match_key(record.get("phone")),
        normalize_address(record.get("address")),
        normalize_clinic_name(record.get("clinic_name")),
    )


def mark_input_conflicts(items):
    """Hold only UUID groups with material clinic-identity contradictions."""
    uuid_rows = defaultdict(list)
    case_rows = defaultdict(set)
    row_fingerprints = defaultdict(list)
    for item in items:
        record = item["record"]
        uid = record.get("uuid", "")
        if uid:
            uuid_rows[uid].append(item)
        row_fingerprints[item["row_fingerprint"]].append(item)

    uuid_conflicts = {}
    uuid_identities = {}
    for uid, group in uuid_rows.items():
        conflicts = set()
        for index, left in enumerate(group):
            for right in group[index + 1:]:
                lrec, rrec = left["record"], right["record"]
                lphone, rphone = tel_match_key(lrec.get("phone")), tel_match_key(rrec.get("phone"))
                lname, rname = normalize_clinic_name(lrec.get("clinic_name")), normalize_clinic_name(rrec.get("clinic_name"))
                laddr, raddr = normalize_address(lrec.get("address")), normalize_address(rrec.get("address"))
                if lphone and rphone and lphone != rphone:
                    conflicts.add("PHONE")
                if lname and rname and fuzz.ratio(lname, rname) < 95:
                    conflicts.add("NAME")
                if laddr and raddr and fuzz.ratio(laddr, raddr) < 95:
                    conflicts.add("ADDRESS")
        uuid_conflicts[uid] = sorted(conflicts)
        uuid_identities[uid] = {
            input_identity(row["record"]) for row in group
        }

    for item in items:
        record = item["record"]
        uid = record.get("uuid", "")
        case_id = record.get("comdesk_case_id", "")
        reasons = []
        if uid and uuid_conflicts.get(uid):
            reasons.append("SOURCE_UUID_IDENTITY_CONFLICT:" + ",".join(uuid_conflicts[uid]))
        item["input_conflict"] = ";".join(reasons)
        duplicate_refs = [
            f"{other['source_file']}:{other['source_row']}"
            for other in row_fingerprints[item["row_fingerprint"]]
            if (other["source_file"], other["source_row"]) != (item["source_file"], item["source_row"])
        ]
        item["exact_row_copies"] = sorted(duplicate_refs)
    return items, uuid_identities, case_rows


def classify_inputs(items, candidate_index):
    from src.master.matching import match_record

    results = []
    for item in items:
        record = item["record"]
        if item["input_conflict"]:
            status, reason, clinic_id, candidates = "HOLD", item["input_conflict"], None, []
        else:
            match = match_record(
                None, record, strict_identity=True, candidate_index=candidate_index
            )
            status = {"MATCHED": "AUTO_MATCH", "AMBIGUOUS": "HOLD", "NEW": "NO_MATCH"}[match.status]
            reason, candidates = match.reason, list(match.candidates)
            clinic_id = candidates[0] if status == "AUTO_MATCH" and len(candidates) == 1 else None
        item["status"] = status
        item["reason"] = reason
        item["clinic_id"] = clinic_id
        item["candidate_clinic_ids"] = candidates
        results.append(item)

    uuid_targets = defaultdict(set)
    clinic_uuids = defaultdict(set)
    for item in results:
        uid, cid = item["record"].get("uuid", ""), item["clinic_id"]
        if uid and cid is not None:
            uuid_targets[uid].add(cid)
            clinic_uuids[cid].add(uid)
    for item in results:
        uid, cid = item["record"].get("uuid", ""), item["clinic_id"]
        if item["status"] == "AUTO_MATCH" and uid and len(uuid_targets[uid]) > 1:
            item.update(status="HOLD", reason="SOURCE_UUID_MAPS_TO_MULTIPLE_CLINICS", clinic_id=None)
        elif item["status"] == "AUTO_MATCH" and cid is not None and len(clinic_uuids[cid]) > 1:
            item.update(status="HOLD", reason="MULTIPLE_SOURCE_UUIDS_TARGET_ONE_CLINIC", clinic_id=None)
    return results


def duplicate_rows(items, exact_file_copies):
    result = list(exact_file_copies)
    by_fingerprint = defaultdict(list)
    by_uuid = defaultdict(list)
    for item in items:
        by_fingerprint[item["row_fingerprint"]].append(item)
        if item["record"].get("uuid"):
            by_uuid[item["record"]["uuid"]].append(item)
    seen = set()
    for kind, groups in (("EXACT_ROW_REPEAT", by_fingerprint), ("REPEATED_SOURCE_UUID_ROWS_RETAINED", by_uuid)):
        for key, rows in groups.items():
            if len(rows) < 2:
                continue
            row_refs = tuple(sorted((row["source_file"], row["source_row"]) for row in rows))
            marker = (kind, row_refs)
            if marker in seen:
                continue
            seen.add(marker)
            for row in rows:
                result.append({
                    "duplicate_type": kind,
                    "source_file": row["source_file"],
                    "source_row": row["source_row"],
                    "source_hash": row["source_hash"],
                    "project_scope": row["project_scope"],
                    "comdesk_uuid": row["record"].get("uuid", ""),
                    "comdesk_case_id": row["record"].get("comdesk_case_id", ""),
                    "clinic_id": row.get("clinic_id") or "",
                    "related_rows": ";".join(
                        "{}:{}".format(other["source_file"], other["source_row"])
                        for other in rows if other is not row
                    ),
                    "decision": "RETAIN_SOURCE_ROW; do not deduplicate by clinic UUID alone",
                })
    return result


def uuid_group_report(items):
    """Summarize repeated source UUIDs without treating repetition itself as an error."""
    grouped = defaultdict(list)
    for item in items:
        uid = str(item["record"].get("uuid") or "").strip()
        if uid:
            grouped[uid].append(item)
    report = []
    for uid, rows in sorted(grouped.items()):
        conflicts = sorted({
            reason.split(":", 1)[1]
            for row in rows for reason in row.get("input_conflict", "").split(";")
            if reason.startswith("SOURCE_UUID_IDENTITY_CONFLICT:")
        })
        identities = {input_identity(row["record"]) for row in rows}
        report.append({
            "comdesk_uuid": uid,
            "source_rows": len(rows),
            "distinct_normalized_identity_tuples": len(identities),
            "source_file_rows": ";".join(sorted(f"{row['source_file']}:{row['source_row']}" for row in rows)),
            "conflicting_fields": ";".join(conflicts),
            "classification": "HOLD_IDENTITY_CONFLICT" if conflicts else "CONSISTENT_REPEATED_UUID",
        })
    return report


def project_rows(items, clinics_by_id):
    output = []
    uuid_clinic_targets = defaultdict(set)
    for clinic in clinics_by_id.values():
        value = str(clinic.get("uuid") or "").strip()
        if value:
            uuid_clinic_targets[value].add(clinic.get("id"))
    for item in items:
        record = item["record"]
        clinic = clinics_by_id.get(item.get("clinic_id")) if item.get("clinic_id") is not None else None
        current_uuid = str((clinic or {}).get("uuid") or "")
        source_uuid = record.get("uuid", "")
        if not source_uuid:
            assignment = "NO_SOURCE_UUID"
        elif current_uuid == source_uuid:
            assignment = "EXISTING_UUID_PRESERVED"
        elif current_uuid:
            assignment = "HOLD_EXISTING_DIFFERENT_UUID"
        elif item["status"] != "AUTO_MATCH":
            assignment = "HOLD_MATCH_NOT_CONFIRMED"
        elif len(uuid_clinic_targets.get(source_uuid, set())) > 0:
            assignment = "HOLD_UUID_ALREADY_BOUND_TO_OTHER_CLINIC"
        elif item.get("input_conflict"):
            assignment = "HOLD_SOURCE_UUID_IDENTITY_CONFLICT"
        else:
            assignment = "UUID_ASSIGNMENT_CANDIDATE"
        output.append({
            "source_file": item["source_file"],
            "source_hash": item["source_hash"],
            "source_row": item["source_row"],
            "project_scope": item["project_scope"],
            "project_scope_source": item["project_scope_source"],
            "Comdesk案件ID": record.get("comdesk_case_id", ""),
            "Comdesk_UUID_candidate": source_uuid,
            "clinic_id_before": item.get("clinic_id") or "",
            "clinic_id_after_if_approved": item.get("clinic_id") or "NO_MATCH",
            "Clinic_UUID_before": current_uuid,
            "Clinic_UUID_after_plan": current_uuid,
            "Clinic_UUID_binding_status": assignment,
            "decision": item["status"],
            "match_reason": item["reason"],
            "candidate_clinic_ids": ";".join(str(value) for value in item["candidate_clinic_ids"]),
            "exact_input_row_references": ";".join(item["exact_row_copies"]),
            "clinic_master_write": "NO",
            "HP_URL_rank_research_change": "NO",
        })
    return output


def write_csv(path, rows, fieldnames=None):
    if fieldnames is None:
        fieldnames = list(dict.fromkeys(key for row in rows for key in row))
    with Path(path).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
