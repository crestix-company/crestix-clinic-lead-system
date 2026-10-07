"""Stage4-D Owner Decision 1: medical_key identity contract (pure functions, no DB).

Separates what ``ClinicStore._project()`` currently does in one step (projection + identity
rewrite) into four independent operations, per the Gate 1.5 design and the Owner Decision
recorded in docs/supabase_migration/22_stage4d_write_inventory_gate.md:

  1. derive_clinic_projection -- pure transformation, no identity decision.
  2. generate_medical_key_for_new_clinic -- only before the first INSERT of a new clinic.
  3. validate_existing_medical_key / resolve_medical_key_transition -- equality-only guard;
     raises on any disallowed transition; never repairs/writes.
  4. refresh_clinic_projection_preserving_identity -- caller-side contract: the returned dict
     never contains "medical_key", so a caller cannot accidentally UPDATE it through this path.

This module is additive and is not yet wired into ``src.master.store.ClinicStore._project()``;
the existing SQLite runtime path is unchanged. It backs the Supabase write adapter
(src.repository.supabase_write_adapter) and is covered by tests/test_stage4d_write_repository.py.
"""
from src.master.matching import medical_key
from src.normalizer.clinic_name import normalize_clinic_name, normalize_person, person_from_owner
from src.normalizer.phone import normalize_phone, tel_match_key
from src.normalizer.address import normalize_address
from src.normalizer.departments import normalize_departments
from src.utils.date_utils import parse_date


class IdentityContractError(Exception):
    """A requested medical_key transition is not allowed under the Owner Decision 1 contract."""


def derive_clinic_projection(data):
    """Pure projection fields, excluding medical_key/uuid/id. ``data`` is the merged
    base+research+manual dict exactly as ClinicStore._project() builds it today.
    Returns (candidate_medical_key, projection_fields_without_identity).
    """
    candidate_key = medical_key(data)
    return candidate_key


def generate_medical_key_for_new_clinic(record, *, is_authoritative_official_source):
    """Only callable before the first INSERT of a new clinic row. Returns the initial
    medical_key, or "" if the source does not supply enough identity to compute one yet
    (e.g. a Comdesk-only new row) -- an empty initial key is a valid, blank starting state,
    not a violation; it simply has nothing to validate against later.
    """
    if not is_authoritative_official_source:
        return ""
    return medical_key(record)


def resolve_medical_key_transition(stored, candidate, *, authoritative):
    """The single source of truth for Owner Decision 1's transition table.

    | stored    | candidate | authoritative | result                                   |
    |-----------|-----------|----------------|------------------------------------------|
    | ""        | ""        | any            | "" (no-op, nothing to complete)          |
    | ""        | non-blank | True           | candidate (identity completion, once)    |
    | ""        | non-blank | False          | raise (completion requires authoritative) |
    | non-blank | == stored | any            | stored (no-op; already correct)          |
    | non-blank | != stored | any            | raise (known -> other known forbidden)   |
    | non-blank | ""        | any            | raise (known -> blank forbidden)         |

    Never mutates anything; the caller decides whether to include "medical_key" in an UPDATE
    at all based on whether this function returned a value different from ``stored``.
    """
    if stored == "":
        if candidate == "":
            return ""
        if not authoritative:
            raise IdentityContractError(
                "medical_keyのblank→known遷移は、確認済みの厚生局等公式sourceによる"
                "一致時のみ許可されています（identity completion）。"
            )
        return candidate
    if candidate == stored:
        return stored
    if candidate == "":
        raise IdentityContractError("既存のmedical_keyをblankへ変更することは禁止されています。")
    raise IdentityContractError(
        "既存のmedical_keyを別の値へ変更することは禁止されています"
        f"（stored={stored!r}, candidate={candidate!r}）。"
    )


def validate_existing_medical_key(stored, candidate):
    """Validation-only entry point for ordinary projection refresh (never authoritative).
    Equivalent to resolve_medical_key_transition(stored, candidate, authoritative=False),
    exposed separately because callers that are not doing identity completion should never
    need to pass an `authoritative` flag at all.
    """
    return resolve_medical_key_transition(stored, candidate, authoritative=False)


def refresh_clinic_projection_preserving_identity(data):
    """Non-identity projection fields only. Mirrors ClinicStore._project()'s column computation
    (store.py:325-364) field-for-field, minus "medical_key" (and never uuid/id) -- the caller
    must resolve the medical_key transition separately via resolve_medical_key_transition and
    decide for itself whether "medical_key" belongs in its own UPDATE/INSERT column list.

    ``data`` is the already-merged base+research+manual dict with "uuid" and "manual_fields"
    set, exactly as ClinicStore._project() builds it before computing `values` -- this function
    does not read/merge from any table itself (no DB access; pure).
    """
    from src.scoring.research_scoring import finalize_result

    data = finalize_result(data)
    n = normalize_clinic_name(data.get("clinic_name"))
    p = normalize_phone(data.get("phone"))
    a = normalize_address(data.get("address"))
    owner = person_from_owner(data.get("owner_name", ""))
    manager = data.get("manager_name", "")
    equal = int(normalize_person(owner) == normalize_person(manager)) if owner and manager else None
    des = parse_date(data.get("designation_date", ""))
    expiry = ""
    if des:
        try:
            expiry = des.replace(year=des.year + 10).isoformat()
        except ValueError:
            expiry = des.replace(year=des.year + 10, day=28).isoformat()
    active = data.get("status") in {"現存", "営業中", "開業中", "稼働中", "true", "True", "1"} and data.get(
        "facility_type"
    ) in {"診療所", "クリニック", "医院", "医科診療所", "歯科診療所"}
    data["owner_manager_equal"] = None if equal is None else bool(equal)
    data["active"] = bool(active)
    data["normalized_departments"] = normalize_departments(data.get("departments", ""))
    return {
        "clinic_name": data.get("clinic_name", ""), "phone": data.get("phone", ""), "address": data.get("address", ""),
        "phone_norm": p, "tel_match_key": tel_match_key(data.get("phone")), "name_norm": n, "name_prefix": n[:2],
        "address_norm": a,
        "prefecture": data.get("prefecture", ""), "medical_type": data.get("medical_type", ""),
        "effective_json": data,
        "active": int(active), "designation_date": des.isoformat() if des else "", "recent_until": expiry,
        "registration_reason": data.get("registration_reason", ""), "owner_equal": equal,
        "age_probability": data.get("age_probability_under_59"), "hp_status": data.get("hp_status", "UNRESEARCHED"),
        "hp_url": data.get("hp_url", ""), "hp_rank": data.get("hp_rank", "UNKNOWN"),
        "signal_count": data["marketing_signal_count"], "hot_status": data["hot_status"],
        "departments_json": data["normalized_departments"], "treatments_json": data.get("treatment_categories", []),
        "signals_json": [s["name"] for s in data.get("marketing_signals", []) if s.get("status") == "CONFIRMED"],
        "maps_presence_status": data.get("maps_presence_status", ""), "maps_profile_url": data.get("maps_profile_url", ""),
        "maps_website_url": data.get("maps_website_url", ""), "maps_match_method": data.get("maps_match_method", ""),
        "maps_checked_at": data.get("maps_checked_at", ""), "exclude_reason": data.get("exclude_reason", ""),
    }
