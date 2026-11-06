"""Fail-closed Comdesk UUID reimport for existing UUID-empty clinics only.

No clinic creation and no changes to name, phone, address, sales state or HP rank.
The returned Comdesk UUID is an external identifier, not a generated Clinic ID.
"""
from __future__ import annotations

from collections import Counter
import re
import unicodedata

from src.services.comdesk import infer_comdesk_columns, record_from_row


def normalized(value, *, phone=False):
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    if phone:
        return "".join(ch for ch in value if ch.isdigit())
    return "".join(ch for ch in value if ch.isalnum())


def records_from_table(table):
    mapping = infer_comdesk_columns(table)
    for key in ("uuid", "clinic_name", "phone"):
        if mapping.get(key) is None:
            raise ValueError("UUID再取込にはUUID・名前・Tel1の各列が必要です。")
    records = []
    for index, raw in enumerate(table.data.values.tolist(), start=2):
        record = record_from_row(raw, mapping)
        records.append({"row_number": index, "uuid": record["uuid"],
                        "clinic_name": record.get("clinic_name", ""),
                        "phone": record.get("phone", ""),
                        "address": record.get("address", "")})
    return records


def build_uuid_only_plan(records, candidate_by_phone, existing_uuids=()):
    """Require unique phone plus matching name or address; ambiguous cases always HOLD.

    candidate_by_phone maps normalized phone to clinic rows; both uuid and identity
    are rechecked inside the database transaction before applying.
    """
    counts = Counter(str(row.get("uuid") or "").strip() for row in records if row.get("uuid"))
    reserved = set(existing_uuids)
    result = []
    for record in records:
        uid = str(record.get("uuid") or "").strip()
        phone = normalized(record.get("phone"), phone=True)
        possible = list(candidate_by_phone.get(phone, ())) if phone else []
        matches = []
        for c in possible:
            if str(c.get("uuid") or "").strip():
                continue
            same_name = bool(normalized(record.get("clinic_name"))) and (
                normalized(record.get("clinic_name")) == normalized(c.get("clinic_name")))
            same_address = bool(normalized(record.get("address"))) and (
                normalized(record.get("address")) == normalized(c.get("address")))
            if same_name or same_address:
                matches.append(int(c["id"]))
        matches = sorted(set(matches))
        if not uid:
            reason = "Comdesk UUIDが空欄です"
        elif counts[uid] != 1:
            reason = "同じCSV内でComdesk UUIDが重複しています"
        elif uid in reserved:
            reason = "Comdesk UUIDが既存医院に登録済みです"
        elif not phone or not normalized(record.get("clinic_name")):
            reason = "医院名・電話番号の照合情報が不足しています"
        elif len(matches) != 1:
            reason = "UUID未付与医院との照合が一意ではありません"
        else:
            reason = ""
        result.append({"row_number": record["row_number"], "uuid": uid,
                       "clinic_id": matches[0] if not reason else None,
                       "status": "UPDATE" if not reason else "HOLD",
                       "reason": reason, "clinic_name": record.get("clinic_name", ""),
                       "phone": record.get("phone", ""), "address": record.get("address", "")})
    return result
