"""Pure local import reconciliation plan. Never writes to Sheets or a database."""
from __future__ import annotations

from collections import Counter
from typing import Mapping, Sequence


ALIASES = {
    "clinic_id": ("clinic_id", "clinic id", "Clinic ID", "医院ID"),
    "clinic_uuid": ("clinic_uuid", "Clinic UUID", "uuid"),
    "clinic_name": ("clinic_name", "医院名", "クリニック名"),
    "case_id": ("案件ID", "GAS案件ID", "gas_case_id", "case_id"),
    "campaign_id": ("campaign_id", "キャンペーンID"),
    "gmail_state": ("gmail_state", "メール状態", "Gmail状態", "状態"),
}


def _get(row: Mapping[str, str], key: str) -> str:
    for name in ALIASES[key]:
        value = row.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def plan_local_import_actions(
    headers: Sequence[str], rows: Sequence[Sequence[str]], *, source_tab: str,
    known_clinics: Mapping[str, Mapping[str, str]],
    local_ledger: Mapping[str, Mapping[str, str]],
) -> tuple[dict[str, object], ...]:
    """Classify full imported rows; ambiguous identity and duplicates always HOLD."""
    source = [dict(zip(headers, row)) for row in rows]
    ids = Counter(_get(row, "clinic_id") for row in source if _get(row, "clinic_id"))
    uuids = Counter(_get(row, "clinic_uuid").casefold() for row in source if _get(row, "clinic_uuid"))
    actions = []
    for index, raw in enumerate(source, start=2):
        clinic_id, clinic_uuid = _get(raw, "clinic_id"), _get(raw, "clinic_uuid")
        key = clinic_uuid.casefold()
        action, reason = "HOLD", "Clinic ID/UUIDが不足しているため確認が必要です"
        master = known_clinics.get(key)
        if clinic_id and clinic_id in ids and ids[clinic_id] > 1:
            reason = "CSV内Clinic ID重複。自動取込を保留しました"
        elif clinic_uuid and uuids[key] > 1:
            reason = "CSV内Clinic UUID重複。自動取込を保留しました"
        elif clinic_id and clinic_uuid and master:
            if str(master.get("clinic_id")) != clinic_id:
                reason = "Clinic IDとClinic UUIDの対応が一致しません"
            else:
                current = local_ledger.get(key)
                comparable = {name: _get(raw, name) for name in ("case_id", "campaign_id", "gmail_state")}
                if current is None:
                    action, reason = "INSERT", "ローカルMock台帳に該当履歴がありません"
                elif all(str(current.get(name, "")) == value for name, value in comparable.items()):
                    action, reason = "NOOP", "ローカルMock台帳と差分なし"
                else:
                    action, reason = "UPDATE", "ローカルMock台帳に更新差分があります"
        elif clinic_uuid and not master:
            reason = "Clinic UUIDをClinic Masterへ一意に照合できません"
        actions.append({"action": action, "source_tab": source_tab, "source_row": index,
                        "clinic_id": clinic_id, "clinic_uuid": clinic_uuid,
                        "gas_case_id": _get(raw, "case_id"), "gmail_state": _get(raw, "gmail_state"),
                        "raw_values": raw, "reason": reason})
    return tuple(actions)

