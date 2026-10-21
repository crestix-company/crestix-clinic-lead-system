"""Read-only Comdesk CSV identity preview. Clinic records are injected by caller."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
import streamlit as st

from src.io.input_loader import load_table
from src.master.comdesk_reconciliation import (
    infer_comdesk_columns,
    input_identity,
    record_from_row,
    source_scope,
    validate_comdesk_mapping,
)
from src.master.matching import ClinicMatchingIndex, match_record
from src.normalizer.address import normalize_address
from src.normalizer.clinic_name import normalize_clinic_name
from src.normalizer.phone import tel_match_key


def classify_rows(table, clinics, *, source_name="uploaded.csv"):
    """Classify Comdesk UUIDs against an injected Clinic fixture/master without writes."""
    mapping = infer_comdesk_columns(table)
    validate_comdesk_mapping(table.headers, mapping)
    if len(table.headers) != 30:
        raise ValueError("Comdesk医院CSVは30列である必要があります")
    parsed = [(list(raw), record_from_row(raw, mapping)) for raw in table.data.values.tolist()]
    identity_counts = Counter(
        (identity[0], identity[2])
        for _, record in parsed
        for identity in (input_identity(record),)
        if identity[0] and identity[2]
    )
    rows = []
    index_rows = []
    for ordinal, clinic in enumerate(clinics, 1):
        row = dict(clinic)
        row.setdefault("id", ordinal)
        row.setdefault("name_norm", normalize_clinic_name(row.get("clinic_name", row.get("name", ""))))
        row.setdefault("address_norm", normalize_address(row.get("address", "")))
        row.setdefault("tel_match_key", tel_match_key(row.get("phone", "")))
        row.setdefault("merged_into", None)
        index_rows.append(row)
    index = ClinicMatchingIndex(index_rows)
    clinics_by_id = {int(clinic["id"]): clinic for clinic in index_rows}
    comdesk_uuid_targets = {}
    for clinic in index_rows:
        uid = str(clinic.get("comdesk_uuid") or clinic.get("canonical_comdesk_uuid") or "").strip()
        if uid:
            comdesk_uuid_targets.setdefault(uid, set()).add(int(clinic["id"]))
    row_keys = Counter()
    plans = []
    for raw, record in parsed:
        project_scope, project_scope_source = source_scope(table, raw, Path(source_name), mapping)
        comdesk_uuid = str(record.get("uuid") or "").strip()
        # Comdesk UUID is intentionally removed from Clinic matching input.
        candidate_record = {**record, "uuid": ""}
        match = match_record(None, candidate_record, strict_identity=True, candidate_index=index)
        clinic_id = match.candidates[0] if match.status == "MATCHED" and len(match.candidates) == 1 else None
        clinic = clinics_by_id.get(clinic_id) if clinic_id is not None else None
        clinic_uuid = str((clinic or {}).get("uuid") or "").strip()
        existing_comdesk_uuid = str((clinic or {}).get("comdesk_uuid") or
                                    (clinic or {}).get("canonical_comdesk_uuid") or "").strip()
        if match.status == "AMBIGUOUS":
            status = "AMBIGUOUS"
        elif match.status == "NEW":
            status = "UNMATCHED"
        elif existing_comdesk_uuid and existing_comdesk_uuid != comdesk_uuid:
            status = "ID_CONFLICT"
        elif existing_comdesk_uuid == comdesk_uuid and existing_comdesk_uuid:
            status = "ALREADY_LINKED"
        else:
            status = "MATCHED_UNIQUE"
        if comdesk_uuid in comdesk_uuid_targets and (
            clinic_id is None or clinic_id not in comdesk_uuid_targets[comdesk_uuid]
        ):
            status = "ID_CONFLICT"
        project = str(project_scope or "").strip()
        case_id = str(record.get("comdesk_case_id") or "").strip()
        project_case_hold = project_scope_source == "FILENAME_ONLY_UNVERIFIED" or not case_id
        identity_key = input_identity(record)
        duplicate_key = (("PROJECT_CASE", project, case_id) if project and case_id
                         else ("UNSCOPED_ROW", comdesk_uuid, *identity_key))
        duplicate_ordinal = row_keys[duplicate_key]
        row_keys[duplicate_key] += 1
        if duplicate_ordinal:
            status = "DUPLICATE"
        reason = match.reason
        if status == "ID_CONFLICT":
            reason = "Clinic UUIDとComdesk UUIDの不一致、またはComdesk UUIDの既存別医院割当"
        elif status == "DUPLICATE":
            reason = "同じプロジェクト・案件ID（またはID欠落時の同一元行識別）の重複候補。元行は保持"
        plans.append({
            "status": status,
            "reason": reason,
            "clinic_id": clinic_id,
            "comdesk_uuid": comdesk_uuid,
            "clinic_uuid": clinic_uuid,
            "existing_comdesk_uuid": existing_comdesk_uuid,
            "project_case_status": "HOLD_NO_PROJECT_OR_CASE_ID" if project_case_hold else "PREVIEW_ONLY",
            "same_name_phone_candidate": bool(identity_key[0] and identity_key[2]
                                               and identity_counts[(identity_key[0], identity_key[2])] > 1),
            "_identity_key": identity_key,
            "_name_phone_key": (identity_key[0], identity_key[2]),
            "project_scope": project or "",
            "project_scope_source": project_scope_source,
            "comdesk_case_id": case_id,
            "raw_row": list(raw),
            "source_row": len(plans) + 2,
        })
    return plans


def summarize(plans):
    counts = Counter(row["status"] for row in plans)
    result = {name: counts.get(name, 0) for name in (
        "MATCHED_UNIQUE", "ALREADY_LINKED", "AMBIGUOUS", "UNMATCHED", "ID_CONFLICT", "DUPLICATE"
    )}
    repeated = {row["_name_phone_key"] for row in plans if row["same_name_phone_candidate"]}
    result["same_name_phone_candidate_groups"] = len(repeated)
    result["same_name_phone_candidate_rows"] = sum(row["same_name_phone_candidate"] for row in plans)
    return result


def render_comdesk_uuid_preview(clinics, *, master_label="合成Clinic Master"):
    st.header("Comdesk CSV・Clinic UUID照合プレビュー")
    st.caption(f"照合対象：{master_label}。READ ONLYプレビューです。Clinic UUID・案件の登録や更新は行いません。")
    uploaded = st.file_uploader("Comdesk医院CSV（30列）", type=["csv"], key="comdesk_uuid_preview_csv")
    if uploaded is None:
        st.info("CSVを選択すると、Clinic UUID候補とHOLD理由を表示します。")
        return
    try:
        table = load_table(uploaded.getvalue(), uploaded.name)
        plans = classify_rows(table, clinics, source_name=uploaded.name)
    except Exception as exc:
        st.error(f"CSVを照合できません: {exc}")
        return
    if not all(row["project_case_status"] != "HOLD_NO_PROJECT_OR_CASE_ID" for row in plans):
        st.warning("プロジェクトIDまたは案件IDがない行があります。営業案件の自動登録はHOLDです。")
    st.write({"CSV行数": len(plans), "案件自動登録": "禁止（プレビューのみ）", **summarize(plans)})
    display = [{
        "元CSV行": row["source_row"],
        "照合状態": row["status"],
        "理由": row["reason"],
        "Clinic内部ID": row["clinic_id"],
        "Clinic UUID": row["clinic_uuid"],
        "Comdesk UUID": row["comdesk_uuid"],
        "Comdesk案件ID": row["comdesk_case_id"],
        "プロジェクトスコープ": row["project_scope"],
        "案件登録判定": row["project_case_status"],
        "医院名＋電話候補": row["same_name_phone_candidate"],
    } for row in plans]
    st.dataframe(display, use_container_width=True, hide_index=True)
    st.caption("原票行はメモリ上のプレビュー結果に保持します。保存・Clinic Master更新・案件登録は行いません。")
