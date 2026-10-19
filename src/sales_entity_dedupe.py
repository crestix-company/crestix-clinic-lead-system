"""Fail-closed, read-only sales-entity deduplication for Comdesk preview/export.

This module does NOT mutate source records, UUIDs or Supabase.
Call plan_sales_entities on the SAME population being exported; do not apply
deduplication only after filtering out the medical/dental counterpart.
Only an explicit safe match is collapsed. HOLD groups must be reviewed.
"""
from collections import defaultdict
from dataclasses import dataclass
from typing import Mapping, Sequence


@dataclass(frozen=True)
class GroupDecision:
    clinic_ids: tuple[int, ...]
    representative_id: int | None
    decision: str  # SINGLE / COLLAPSE / HOLD
    reason: str
    preserved_uuid: str


def _val(row: Mapping, name: str) -> str:
    return str(row.get(name) or "").strip()


def plan_sales_entities(rows: Sequence[Mapping]) -> list[GroupDecision]:
    """Plan a non-destructive export; never manufacture/reassign UUIDs.

    Requires preexisting *normalized* name/address/phone from clinic master.
    The caller must resolve HOLD before Comdesk output; no silent fallback.
    """
    ids = [int(r["id"]) for r in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate input clinic IDs")

    groups: dict[tuple, list[Mapping]] = defaultdict(list)
    for row in rows:
        cid = int(row["id"])
        name, address, phone = (_val(row, k) for k in
                                ("name_norm", "address_norm", "phone_norm"))
        # Missing evidence cannot be used to identify another clinic.
        key = ("match", name, address, phone) if all((name, address, phone)) else ("solo", cid)
        groups[key].append(row)

    decisions = []
    for members in groups.values():
        members = sorted(members, key=lambda r: int(r["id"]))
        clinic_ids = tuple(int(r["id"]) for r in members)
        uuids = {_val(r, "uuid") for r in members} - {""}
        if len(members) == 1:
            row = members[0]
            decisions.append(GroupDecision(clinic_ids, int(row["id"]), "SINGLE", "unique", _val(row, "uuid")))
            continue

        types = {_val(r, "medical_type") for r in members}
        # Do not collapse multiple medical or multiple dental registrations.
        if len(members) != 2 or types != {"医科", "歯科"}:
            decisions.append(GroupDecision(clinic_ids, None, "HOLD", "ambiguous registration / medical types", ""))
        elif len(uuids) > 1:
            decisions.append(GroupDecision(clinic_ids, None, "HOLD", "conflicting existing UUIDs", ""))
        else:
            # Prefer the member carrying the existing Comdesk UUID.
            rep = next((r for r in members if _val(r, "uuid")), members[0])
            decisions.append(GroupDecision(clinic_ids, int(rep["id"]), "COLLAPSE",
                                           "same normalized name/address/phone, medical + dental",
                                           next(iter(uuids), "")))
    return sorted(decisions, key=lambda d: d.clinic_ids[0])


def approved_representative_ids(decisions: Sequence[GroupDecision]) -> list[int]:
    """Prevent accidental export if any collision requires human review."""
    holds = [d.clinic_ids for d in decisions if d.decision == "HOLD"]
    if holds:
        raise ValueError(f"dedupe review required for {len(holds)} group(s): {holds[:5]}")
    return [d.representative_id for d in decisions if d.representative_id is not None]
