"""Fail-closed clinic-to-sales-entity matching shared by Comdesk workflows.

This module only plans matches. Persistence belongs to the repository layer and
must use the unique constraints in the accompanying Supabase migration.
Comdesk UUIDs are source identifiers: this module never creates or rewrites one.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence
import unicodedata


def _text(row: Mapping, key: str) -> str:
    return str(row.get(key) or "").strip()


def _normalized(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return "".join(ch for ch in value if not ch.isspace() and ch not in "-‐‑‒–—―〒、，,")


def _identity_conflicts(source: Mapping, candidate: Mapping) -> tuple[str, ...]:
    conflicts = []
    for key, label in (("name", "clinic name"), ("address", "address"), ("phone", "phone")):
        left, right = _normalized(_text(source, key)), _normalized(_text(candidate, key))
        if left and right and left != right:
            conflicts.append(label)
    left_code, right_code = _text(source, "medical_code"), _text(candidate, "medical_code")
    if left_code and right_code and left_code != right_code:
        conflicts.append("medical institution code")
    return tuple(conflicts)


@dataclass(frozen=True)
class IdentityDecision:
    status: str  # MATCH / NEW / HUMAN_REVIEW / HOLD
    clinic_id: int | None
    common_clinic_id: int | None
    rule: str
    reason: str
    preserved_comdesk_uuid: str


def resolve_clinic_identity(source: Mapping, candidates: Sequence[Mapping]) -> IdentityDecision:
    """Resolve one imported row in the required evidence priority order."""
    source_uuid = _text(source, "uuid")
    source_code = _text(source, "medical_code")

    def choose(rows: list[Mapping], rule: str) -> IdentityDecision:
        ids = {int(row["id"]) for row in rows}
        entities = {int(row["common_clinic_id"]) for row in rows if row.get("common_clinic_id") is not None}
        if len(ids) != 1 or len(entities) > 1:
            return IdentityDecision("HOLD", None, None, rule, "複数clinic/entity候補が競合", "")
        target = rows[0]
        conflicts = _identity_conflicts(source, target)
        candidate_uuid = _text(target, "uuid")
        if conflicts or (source_uuid and candidate_uuid and source_uuid != candidate_uuid):
            reason = "医院情報がUUIDと矛盾: " + ", ".join(conflicts) if conflicts else "既存UUIDを上書きする照合"
            return IdentityDecision("HOLD", int(target["id"]), next(iter(entities), None), rule, reason, candidate_uuid)
        common_id = next(iter(entities), None)
        return IdentityDecision("MATCH", int(target["id"]), common_id, rule,
                                "既存医院を再利用", candidate_uuid or source_uuid)

    if source_uuid:
        exact = [row for row in candidates if _text(row, "uuid") == source_uuid]
        if exact:
            return choose(exact, "COMDESK_UUID")
        # A candidate known to share the common entity cannot claim a second UUID.
        same_entity = [row for row in candidates if source.get("common_clinic_id") is not None
                       and row.get("common_clinic_id") == source.get("common_clinic_id")
                       and _text(row, "uuid") and _text(row, "uuid") != source_uuid]
        if same_entity:
            return IdentityDecision("HOLD", None, int(source["common_clinic_id"]), "COMDESK_UUID",
                                    "同一共通医院IDに異なるUUID", "")

    if source_code:
        exact_code = [row for row in candidates if _text(row, "medical_code") == source_code]
        if exact_code:
            return choose(exact_code, "MEDICAL_CODE")

    def exact_fields(key: str) -> list[Mapping]:
        value = _normalized(_text(source, key))
        return [row for row in candidates if value and _normalized(_text(row, key)) == value]

    # Exact source name + address + phone has priority over normalized evidence.
    exact_name_address = [row for row in candidates
                          if _text(source, "name") and _text(source, "address")
                          and _text(row, "name") == _text(source, "name")
                          and _text(row, "address") == _text(source, "address")]
    if exact_name_address:
        phone_source = _normalized(_text(source, "phone"))
        matching = [row for row in exact_name_address
                    if phone_source and _normalized(_text(row, "phone")) == phone_source]
        if matching:
            return choose(matching, "NAME_ADDRESS_PHONE")
        return IdentityDecision("HUMAN_REVIEW", None, None, "NAME_ADDRESS_PHONE",
                                "名称・住所候補の電話が一致しない、または不足", "")

    norm_name = _normalized(_text(source, "name"))
    norm_address = _normalized(_text(source, "address"))
    normalized = [row for row in candidates
                  if norm_name and norm_address
                  and _normalized(_text(row, "name")) == norm_name
                  and _normalized(_text(row, "address")) == norm_address]
    if normalized:
        return IdentityDecision("HUMAN_REVIEW", None, None, "NORMALIZED_NAME_ADDRESS",
                                "正規化一致候補は確認待ち", "")
    if exact_fields("name") and exact_fields("phone"):
        return IdentityDecision("HUMAN_REVIEW", None, None, "PARTIAL_FIELDS", "部分一致候補が複数", "")
    return IdentityDecision("NEW", None, None, "NO_MATCH", "照合候補なし。新規UUIDは発行しない", "")


def validate_entity_uuid_assignments(rows: Sequence[Mapping]) -> None:
    """Raise if a planned dataset assigns conflicting UUIDs to one entity."""
    by_entity: dict[int, set[str]] = {}
    by_uuid: dict[str, set[int]] = {}
    for row in rows:
        entity = row.get("common_clinic_id")
        uid = _text(row, "uuid")
        if entity is None or not uid:
            continue
        by_entity.setdefault(int(entity), set()).add(uid)
        by_uuid.setdefault(uid, set()).add(int(entity))
    if any(len(values) > 1 for values in by_entity.values()):
        raise ValueError("HOLD: one common clinic ID has multiple Comdesk UUIDs")
    if any(len(values) > 1 for values in by_uuid.values()):
        raise ValueError("HOLD: one Comdesk UUID maps to multiple common clinic IDs")


def project_export_rows(rows: Sequence[Mapping], project_key: str) -> list[dict]:
    """Return one row per clinic entity in a project, retaining existing UUIDs."""
    selected: dict[tuple[str, int], Mapping] = {}
    for row in rows:
        common_id = row.get("common_clinic_id")
        # Unlinked legacy rows stay separate until identity review approves linkage.
        identity = ("entity", int(common_id)) if common_id is not None else ("clinic", int(row["id"]))
        key = (project_key, identity[1])
        if key in selected:
            previous = selected[key]
            if _text(previous, "uuid") and _text(row, "uuid") and _text(previous, "uuid") != _text(row, "uuid"):
                raise ValueError("HOLD: project entity has conflicting Comdesk UUIDs")
            if not _text(previous, "uuid") and _text(row, "uuid"):
                selected[key] = row
        else:
            selected[key] = row
    result = [dict(row) for row in selected.values()]
    validate_entity_uuid_assignments(result)
    return result
