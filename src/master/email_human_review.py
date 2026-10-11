"""Email Human Review domain contract, separate from HP Human Review."""
from __future__ import annotations

from src.master.email_sales_eligibility import HARD_BLOCK_ROLES, normalize_role

HUMAN_DECISIONS = ("TRUE_POSITIVE", "FALSE_POSITIVE", "UNCERTAIN")
DECISION_LABELS = {
    "TRUE_POSITIVE": "使用可（対象医院のEmail）",
    "FALSE_POSITIVE": "使用不可（対象医院のEmailではない）",
    "UNCERTAIN": "要確認",
}


def true_positive_allowed(candidate: dict) -> bool:
    """UI guard. Persistence/rule layer remains authoritative."""
    role = normalize_role(candidate.get("role"))
    blocks = {str(v).upper() for v in candidate.get("hard_block_codes") or ()}
    if str(candidate.get("email_ownership") or candidate.get("ownership") or "").upper() == "THIRD_PARTY_OWNED":
        return False
    if candidate.get("clinic_name_match") is False or candidate.get("prefecture_match") is False or candidate.get("city_match") is False:
        return False
    return role not in HARD_BLOCK_ROLES and not blocks.intersection({
        "THIRD_PARTY_OWNED", "CLINIC_IDENTITY_MISMATCH", "LOCATION_IDENTITY_MISMATCH",
    })


def review_snapshot(candidate: dict) -> dict:
    keys = (
        "candidate_id", "email", "discovery_source", "source_url", "search_query",
        "auto_identity_decision", "confidence", "role", "ownership",
        "email_ownership", "source_owner_type",
        "verification_status", "clinic_name_match", "prefecture_match", "city_match",
        "verification_reason", "rejection_reason", "hard_block_codes", "rule_version",
        "researched_at",
    )
    return {key: candidate.get(key) for key in keys}
