"""Fail-closed reconciliation of Comdesk CRM and clinic export snapshots.

The Comdesk ``履歴`` column is a per-clinic free-text snapshot. It is never
expanded into fabricated call events. CSV adapters normalize their 30-column
headers to the field names consumed here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import csv
from pathlib import Path
import unicodedata
from typing import Mapping, Sequence


IDENTITY_FIELDS = ("clinic_name", "address", "phone")
SALES_FIELDS = ("progress", "last_call_at", "last_call_by", "call_count")


def _text(value) -> str:
    return str(value or "").strip()


def _normalized(value, *, phone=False) -> str:
    value = unicodedata.normalize("NFKC", _text(value)).casefold()
    allowed = str.isalnum
    if phone:
        return "".join(ch for ch in value if ch.isdigit())
    return "".join(ch for ch in value if allowed(ch))


def normalize_projects(value) -> tuple[str, ...]:
    """Parse the Comdesk multiline project membership field without duplicates."""
    text = _text(value).replace("\r\n", "\n").replace("\r", "\n")
    result = []
    seen = set()
    for line in text.split("\n"):
        project = line.strip()
        if project and project not in seen:
            seen.add(project)
            result.append(project)
    return tuple(result)


def _history_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _same_identity(left: Mapping, right: Mapping) -> tuple[bool, str]:
    agreements = 0
    for field in IDENTITY_FIELDS:
        a = _normalized(left.get(field), phone=field == "phone")
        b = _normalized(right.get(field), phone=field == "phone")
        if not a or not b:
            continue
        if a != b:
            return False, f"{field}がComdesk CSV間またはclinic masterと不一致"
        agreements += 1
    if agreements < 2:
        return False, "医院本人確認に使える一致項目が2つ未満"
    return True, ""


@dataclass(frozen=True)
class ComdeskSnapshotPlan:
    action: str  # INSERT / UPDATE / UNCHANGED / HOLD
    comdesk_uuid: str
    clinic_id: int | None
    common_clinic_id: int | None
    project_keys: tuple[str, ...]
    sales_state: Mapping
    history_text: str
    history_hash: str
    history_action: str  # INITIAL / CHANGE / UNCHANGED / EMPTY / HOLD
    source_snapshot_at: str
    source_batch_key: str
    reason: str = ""


def _hold(uuid: str, batch_key: str, snapshot_at: str, reason: str,
          sales_state: Mapping | None = None, history_text: str = "",
          clinic_id=None, common_id=None) -> ComdeskSnapshotPlan:
    return ComdeskSnapshotPlan("HOLD", uuid, clinic_id, common_id, (), dict(sales_state or {}),
                               history_text, _history_hash(history_text), "HOLD", snapshot_at,
                               batch_key, reason)


def plan_comdesk_snapshot_pair(
    sales_rows: Sequence[Mapping],
    clinic_rows: Sequence[Mapping],
    clinics_by_uuid: Mapping[str, Mapping | Sequence[Mapping]],
    existing_by_common_id: Mapping[int, Mapping],
    *,
    source_snapshot_at: str,
    source_batch_key: str,
) -> list[ComdeskSnapshotPlan]:
    """Join two Comdesk exports by UUID, verify identities, and plan history diffs."""
    batch_key = _text(source_batch_key)
    snapshot_at = _text(source_snapshot_at)
    if not batch_key or not snapshot_at:
        raise ValueError("source_batch_key and source_snapshot_at are required")
    try:
        incoming_time = datetime.fromisoformat(snapshot_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("source_snapshot_at must be an ISO timestamp") from exc

    def grouped(rows):
        result = {}
        for row in rows:
            uid = _text(row.get("uuid"))
            if uid:
                result.setdefault(uid, []).append(row)
            else:
                result.setdefault("", []).append(row)
        return result

    sales = grouped(sales_rows)
    clinics = grouped(clinic_rows)
    all_uuids = sorted((set(sales) | set(clinics)) - {""})
    plans = []
    for uid in all_uuids:
        sales_group = sales.get(uid, [])
        clinic_group = clinics.get(uid, [])
        if len(sales_group) != 1 or len(clinic_group) != 1:
            plans.append(_hold(uid, batch_key, snapshot_at,
                               "UUIDが片方のCSVにないか、同一CSV内で重複しています"))
            continue
        source, profile = sales_group[0], clinic_group[0]
        identity_ok, reason = _same_identity(source, profile)
        if not identity_ok:
            plans.append(_hold(uid, batch_key, snapshot_at, reason,
                               source, _text(profile.get("history"))))
            continue

        clinic_candidates = clinics_by_uuid.get(uid, ())
        if isinstance(clinic_candidates, Mapping):
            clinic_candidates = [clinic_candidates]
        if len(clinic_candidates) != 1:
            plans.append(_hold(uid, batch_key, snapshot_at,
                               "UUIDに対応するclinic ID・共通医院IDが一意ではありません",
                               source, _text(profile.get("history"))))
            continue
        master = clinic_candidates[0]
        clinic_id = int(master.get("clinic_id") or master.get("id") or 0)
        common_id = int(master.get("common_clinic_id") or 0)
        current_uuid = _text(master.get("canonical_comdesk_uuid"))
        if clinic_id <= 0 or common_id <= 0 or (current_uuid and current_uuid != uid):
            plans.append(_hold(uid, batch_key, snapshot_at,
                               "医院IDが未確定、または既存Comdesk UUIDと不一致です",
                               source, _text(profile.get("history")), clinic_id or None, common_id or None))
            continue

        identity_ok, reason = _same_identity(profile, master)
        if not identity_ok:
            plans.append(_hold(uid, batch_key, snapshot_at, reason, source,
                               _text(profile.get("history")), clinic_id, common_id))
            continue

        state = {field: source.get(field) for field in SALES_FIELDS}
        projects = normalize_projects(source.get("projects"))
        history_text = _text(profile.get("history"))
        old = existing_by_common_id.get(common_id)
        if old is None:
            action = "INSERT"
            history_action = "INITIAL" if history_text else "EMPTY"
        else:
            if int(old.get("clinic_id") or 0) != clinic_id or _text(old.get("comdesk_uuid")) != uid:
                plans.append(_hold(uid, batch_key, snapshot_at,
                                   "既存スナップショットのclinic IDまたはUUIDが一致しません",
                                   source, history_text, clinic_id, common_id))
                continue
            old_time_text = _text(old.get("source_snapshot_at"))
            incoming_compare = incoming_time
            try:
                old_time = datetime.fromisoformat(old_time_text.replace("Z", "+00:00"))
                if old_time.tzinfo is None and incoming_time.tzinfo is not None:
                    old_time = old_time.replace(tzinfo=incoming_time.tzinfo)
                elif old_time.tzinfo is not None and incoming_time.tzinfo is None:
                    incoming_compare = incoming_time.replace(tzinfo=old_time.tzinfo)
            except (AttributeError, TypeError, ValueError):
                plans.append(_hold(uid, batch_key, snapshot_at,
                                   "既存スナップショット日時が解釈できません",
                                   source, history_text, clinic_id, common_id))
                continue
            else:
                if incoming_compare < old_time:
                    plans.append(_hold(uid, batch_key, snapshot_at,
                                       "前回より古いスナップショットのため状態を巻き戻しません",
                                       source, history_text, clinic_id, common_id))
                    continue
                same_state = dict(old.get("sales_state") or {}) == state \
                    and tuple(old.get("project_keys") or ()) == projects \
                    and _text(old.get("history_text")) == history_text
                if incoming_compare == old_time and not same_state:
                    plans.append(_hold(uid, batch_key, snapshot_at,
                                       "同一スナップショット日時で内容が異なります",
                                       source, history_text, clinic_id, common_id))
                    continue
                action = "UNCHANGED" if same_state else "UPDATE"
                old_history = _text(old.get("history_text"))
                history_action = "UNCHANGED" if old_history == history_text else "CHANGE"

        plans.append(ComdeskSnapshotPlan(
            action, uid, clinic_id, common_id, projects, state, history_text,
            _history_hash(history_text), history_action, snapshot_at, batch_key,
        ))

    # UUID-less rows cannot be paired and are held explicitly instead of silently dropped.
    for _ in range(max(len(sales.get("", [])), len(clinics.get("", [])))):
        plans.append(_hold("", batch_key, snapshot_at, "Comdesk由来UUIDが空欄です。自動生成・氏名だけの結合は禁止です"))
    return plans


def snapshot_batch_key(sales_file_sha256: str, clinic_file_sha256: str) -> str:
    """Stable key for reimport of the same pair of CSV files."""
    value = f"comdesk-snapshot-v1|{sales_file_sha256}|{clinic_file_sha256}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def read_snapshot_csv(path: str | Path, column_map: Mapping[str, str], *, encoding: str) -> list[dict]:
    """Read one Comdesk CSV with a caller-verified header map.

    Keys in column_map are normalized field names; values must be exact source
    headers observed in the supplied export. Explicit encoding is mandatory so
    a CP932/UTF-8 guess cannot silently corrupt identity fields.
    """
    required = {"uuid", *IDENTITY_FIELDS}
    missing = required - set(column_map)
    if missing:
        raise ValueError(f"column_map missing required normalized fields: {sorted(missing)}")
    with open(path, "r", encoding=encoding, newline="") as stream:
        reader = csv.DictReader(stream)
        headers = reader.fieldnames or []
        if len(headers) != 30:
            raise ValueError(f"expected the documented 30-column export, got {len(headers)} columns")
        absent = set(column_map.values()) - set(headers)
        if absent:
            raise ValueError(f"mapped source headers absent from CSV: {sorted(absent)}")
        return [{normalized: row.get(source_header, "") for normalized, source_header in column_map.items()}
                for row in reader]
