"""Safe, previewable synchronization of Comdesk's formal 30-column export.

This path is deliberately separate from import_comdesk(): it never creates clinics or
changes either UUID. A Comdesk UUID must resolve through existing provenance or the
Comdesk identity map to exactly one Clinic internal ID.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from collections import Counter
import hashlib
import json

FIELD_HEADERS = {
    "hp_rank": "HPランク",
    "hp_url": "URL",
    "email": "メールアドレス",
    "business_hours": "診療日",
    "phone": "Tel1",
    "address": "住所１",
    "clinic_name": "名前",
}
SUPPORTED_FIELDS = {"hp_rank", "email"}


@dataclass
class SyncChange:
    row_number: int
    clinic_id: int | None
    comdesk_uuid: str
    status: str
    reason: str
    changes: dict


def plan_sync(table, repository):
    """Create a stable preview. Unimplemented/unsafe destinations are explicitly HOLD."""
    if not {"UUID", "HPランク", "URL", "メールアドレス"}.issubset(table.headers):
        raise ValueError("Step7同期にはComdesk 30列形式（UUID・HPランク・メールアドレス）が必要です。")
    rows = table.data.values.tolist()
    uuids = [str(row[table.headers.index("UUID")] or "").strip() for row in rows]
    duplicate_uuids = {uid for uid, count in Counter(uid for uid in uuids if uid).items() if count > 1}
    clinics = repository.comdesk_sync_clinics(uuids)
    counts = {"AUTO_UPDATE": 0, "NO_CHANGE": 0, "HOLD": 0}
    item_counts = {key: 0 for key in FIELD_HEADERS}
    plan = []
    for index, raw in enumerate(rows):
        uid = uuids[index]
        candidate = clinics.get(uid, []) if uid else []
        if uid in duplicate_uuids:
            reason = "同一CSV内でComdesk UUIDが重複"
            decision = SyncChange(index + 2, None, uid, "HOLD", reason, {})
        elif len(candidate) != 1:
            reason = "Comdesk UUID空欄" if not uid else ("対応未登録" if not candidate else "複数Clinicへ対応")
            decision = SyncChange(index + 2, None, uid, "HOLD", reason, {})
        else:
            clinic = candidate[0]
            changes = {}
            held = []
            for field, header in FIELD_HEADERS.items():
                value = str(raw[table.headers.index(header)] or "").strip()
                if not value:
                    continue
                current = str(clinic.get(field) or "").strip()
                if value == current:
                    continue
                changes[field] = {"before": current, "after": value}
                if field not in SUPPORTED_FIELDS:
                    held.append(field)
                    continue
                if field == "hp_rank":
                    if clinic.get("hp_rank_manual") or value.upper() not in {"A", "B", "C", "D", "NO_HP", "UNKNOWN"}:
                        held.append(field)
                        continue
                    value = value.upper()
                    changes[field]["after"] = value
                if field == "hp_url":
                    held.append(field)
                    continue
            if held:
                decision = SyncChange(index + 2, int(clinic["id"]), uid, "HOLD",
                                      "保存先未定義または手動値と競合: " + ", ".join(held), changes)
            elif changes:
                decision = SyncChange(index + 2, int(clinic["id"]), uid, "AUTO_UPDATE", "完全一致UUID", changes)
            else:
                decision = SyncChange(index + 2, int(clinic["id"]), uid, "NO_CHANGE", "変更なし", {})
        counts[decision.status] += 1
        for field in decision.changes:
            item_counts[field] += 1
        plan.append(asdict(decision))
    payload = {"rows": plan, "counts": counts, "item_counts": item_counts,
               "file_hash": hashlib.sha256(json.dumps([list(table.headers), table.data.values.tolist()], ensure_ascii=False, separators=(",", ":")).encode()).hexdigest(),
               "headers": list(table.headers)}
    return payload


def apply_sync(repository, plan):
    """Apply only candidates from a freshly recomputed preview; repository is transactional."""
    changes = [row for row in plan["rows"] if row["status"] == "AUTO_UPDATE"]
    return repository.apply_comdesk_sync(changes, plan["file_hash"])
